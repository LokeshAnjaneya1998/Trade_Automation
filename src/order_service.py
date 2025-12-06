import logging
import os
import threading
import time
from datetime import datetime
from typing import Tuple

import pytz

from src.option_selection import OptionSelection, select_nifty_option_for_signal
from src.calendar_loader import get_nse_trading_calendar_for_current_year
from src.premarket.services import OptionChainService
from src.expiry_utils import fyers_nifty_option_symbol


logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# Shared trading context
TRADING_CALENDAR = get_nse_trading_calendar_for_current_year()
option_chain_service = OptionChainService(symbol="NIFTY")

# Option chain cache
OPTION_CHAIN_CACHE = {"ts": 0.0, "data": None}
_OPTION_CHAIN_LOCK = threading.Lock()
_OPTION_CHAIN_INFLIGHT = False
_OPTION_CHAIN_LAST_ERROR_TS = 0.0
_OPTION_CHAIN_HAD_ERROR = False


# -------------------- Alert parsing --------------------

def parse_simple_alert(text: str) -> Tuple[str | None, str | None, str | None]:
    """
    Parse simple string alerts like:
      BUY_CALL_BREAKOUT, BUY_CALL_REVERSAL, BUY_PUT_BREAKOUT, BUY_PUT_REVERSAL,
      SELL_CALL_EXIT, SELL_PUT_EXIT
    Returns (side, direction, setup_type) or (None, None, None) if unknown.
    """
    t = (text or "").strip().upper()

    if t in ("BUY_CALL_BREAKOUT", "BUY_CE_BREAKOUT"):
        return "buy", "LONG_CALL", "BREAKOUT"
    if t in ("BUY_CALL_REVERSAL", "BUY_CE_REVERSAL"):
        return "buy", "LONG_CALL", "REVERSAL"

    if t in ("BUY_PUT_BREAKOUT", "BUY_PE_BREAKOUT"):
        return "buy", "LONG_PUT", "BREAKOUT"
    if t in ("BUY_PUT_REVERSAL", "BUY_PE_REVERSAL"):
        return "buy", "LONG_PUT", "REVERSAL"
    if t in ("SELL_CALL_EXIT", "SELL_CE_EXIT", "EXIT_CALL", "EXIT_CE"):
        return "sell", "LONG_CALL", "EXIT"
    if t in ("SELL_PUT_EXIT", "SELL_PE_EXIT", "EXIT_PUT", "EXIT_PE"):
        return "sell", "LONG_PUT", "EXIT"
    return None, None, None


# -------------------- Option chain caching --------------------

def _refresh_option_chain_async():
    """
    Fire-and-forget refresh of NSE option chain. Does not block webhook.
    """
    global _OPTION_CHAIN_INFLIGHT, _OPTION_CHAIN_LAST_ERROR_TS, _OPTION_CHAIN_HAD_ERROR
    with _OPTION_CHAIN_LOCK:
        if _OPTION_CHAIN_INFLIGHT:
            return
        _OPTION_CHAIN_INFLIGHT = True

    def _worker():
        global _OPTION_CHAIN_INFLIGHT, _OPTION_CHAIN_LAST_ERROR_TS, _OPTION_CHAIN_HAD_ERROR
        try:
            data = option_chain_service._fetch_raw()
            OPTION_CHAIN_CACHE["data"] = data
            OPTION_CHAIN_CACHE["ts"] = time.time()
            if _OPTION_CHAIN_HAD_ERROR:
                logger.info("Option chain cache refreshed after failure")
                _OPTION_CHAIN_HAD_ERROR = False
        except Exception as exc:
            now = time.time()
            if now - _OPTION_CHAIN_LAST_ERROR_TS > 300:
                logger.warning("Option chain refresh failed: %s", exc)
                _OPTION_CHAIN_LAST_ERROR_TS = now
                _OPTION_CHAIN_HAD_ERROR = True
            else:
                logger.debug("Option chain refresh failed (suppressed): %s", exc)
        finally:
            _OPTION_CHAIN_INFLIGHT = False

    threading.Thread(target=_worker, daemon=True).start()


def get_cached_option_chain(max_age_sec: int = 30):
    """
    Return cached option-chain data if fresh enough.
    If stale/missing, trigger async refresh and return last known (may be None).
    """
    now = time.time()
    cached = OPTION_CHAIN_CACHE.get("data")
    ts = OPTION_CHAIN_CACHE.get("ts") or 0
    age = now - ts
    if cached and age <= max_age_sec:
        return cached
    _refresh_option_chain_async()
    return cached


def _schedule_option_chain_refresh(interval_sec: int = 20):
    """
    Periodically refresh option chain so webhook paths remain warm.
    """
    def _tick():
        _refresh_option_chain_async()
        _schedule_option_chain_refresh(interval_sec)

    t = threading.Timer(interval_sec, _tick)
    t.daemon = True
    t.start()


# Prime the cache on import
_refresh_option_chain_async()
if not os.getenv("ORDER_SERVICE_DISABLE_TIMER"):
    _schedule_option_chain_refresh()


# -------------------- Selection helpers --------------------

def fetch_nifty_spot_price(fyers_integration) -> float:
    """
    Fetch spot; prefer cached option-chain underlying to avoid extra quote latency.
    """
    cached = get_cached_option_chain()
    if cached:
        try:
            underlying = (
                cached.get("records", {}).get("underlyingValue")
                or cached.get("filtered", {}).get("data", [{}])[0].get("underlyingValue")
            )
            if underlying:
                return float(underlying)
        except Exception:
            pass

    fyers = fyers_integration.get_fyers_instance()
    resp = fyers.quotes({"symbols": "NSE:NIFTY50-INDEX"})
    if resp.get("s") != "ok":
        raise RuntimeError(f"Error fetching NIFTY quote: {resp}")

    data = resp.get("d") or []
    v = data[0].get("v", {}) if data else {}
    lp = v.get("lp")
    if lp is None:
        raise RuntimeError(f"Last price missing in quote payload: {resp}")
    return float(lp)


def choose_nifty_option_from_signal(
    spot_price: float,
    direction: str,
    setup_type: str,
    calendar=None,
):
    now_ist = datetime.now(IST)
    calendar = calendar or TRADING_CALENDAR

    selection = select_nifty_option_for_signal(
        now_ist=now_ist,
        spot_price=spot_price,
        direction=direction,   # "LONG_CALL" / "LONG_PUT"
        setup_type=setup_type, # "BREAKOUT" / "REVERSAL"
        calendar=calendar,
        expiry_weekday=1,      # legacy Tuesday expectation in this codebase
        strike_step=50,
        underlying="NIFTY",
        exchange_prefix="NSE:",
    )
    return selection


def select_option_from_chain(
    *,
    spot_price: float,
    direction: str,
    setup_type: str,
    calendar,
    strike_step: int = 50,
    search_window: int = 200,
) -> OptionSelection | None:
    """
    Pick the most liquid strike near ATM using NSE option chain OI/volume.
    Falls back to calendar-based selection if anything goes wrong.
    """
    raw = get_cached_option_chain()
    if not raw:
        return None

    filtered = raw.get("filtered", {}) or {}
    rows = filtered.get("data") or raw.get("records", {}).get("data", [])
    expiry_str = filtered.get("expiryDate") or (raw.get("records", {}).get("expiryDates") or [None])[0]

    if not rows or not expiry_str:
        logger.warning("Option chain missing rows/expiry; falling back to calendar selection.")
        return None

    try:
        expiry_d = datetime.strptime(expiry_str, "%d-%b-%Y").date()
    except Exception as exc:
        logger.warning("Unable to parse option-chain expiry %r: %s", expiry_str, exc)
        return None

    atm = int(round(spot_price / strike_step) * strike_step)
    lower = atm - search_window
    upper = atm + search_window
    leg_key = "CE" if direction == "LONG_CALL" else "PE"

    best = None
    for row in rows:
        strike = row.get("strikePrice")
        if strike is None or strike < lower or strike > upper:
            continue
        leg = row.get(leg_key)
        if not leg:
            continue

        oi = float(leg.get("openInterest") or 0)
        vol = float(leg.get("totalTradedVolume") or 0)
        ltp = float(leg.get("lastPrice") or 0)
        score = oi * 0.7 + vol * 0.3  # liquidity + participation

        if best is None or score > best["score"]:
            best = {"strike": int(strike), "oi": oi, "vol": vol, "ltp": ltp, "score": score}

    if not best:
        logger.warning("Option chain scan produced no candidates; falling back to calendar selection.")
        return None

    opt_type = "CE" if direction == "LONG_CALL" else "PE"
    symbol = fyers_nifty_option_symbol(
        underlying="NIFTY",
        expiry_d=expiry_d,
        strike=best["strike"],
        opt_type=opt_type,
        calendar=calendar,
        expiry_weekday=1,
        exchange_prefix="NSE:",
    )

    notes = (
        f"OI-based strike={best['strike']} ({leg_key}) oi={best['oi']:.0f} "
        f"vol={best['vol']:.0f} ltp={best['ltp']:.2f} expiry={expiry_d}"
    )

    return OptionSelection(
        direction=direction,
        setup_type=setup_type,
        strike_style="ATM",
        strike=best["strike"],
        expiry_date=expiry_d,
        symbol=symbol,
        notes=notes,
    )


def build_order_details_from_signal(
    fyers_integration,
    direction: str,
    setup_type: str,
    spot_price: float | None = None,
    side: str = "buy",
):
    selection_setup = setup_type if setup_type in ("BREAKOUT", "REVERSAL") else "BREAKOUT"
    if spot_price is None:
        spot_price = fetch_nifty_spot_price(fyers_integration)

    selection = select_option_from_chain(
        spot_price=spot_price,
        direction=direction,
        setup_type=selection_setup,
        calendar=TRADING_CALENDAR,
    )

    source = "option_chain"
    if selection is None:
        selection = choose_nifty_option_from_signal(
            spot_price=spot_price,
            direction=direction,
            setup_type=selection_setup,
            calendar=TRADING_CALENDAR,
        )
        source = "calendar"

    opt_type = "CE" if direction == "LONG_CALL" else "PE"
    fyers_side = 1 if str(side).lower() == "buy" else -1

    order_details = {
        "symbol": selection.symbol,
        "qty": 75,               # you can parameterize this
        "type": 2,               # MARKET
        "side": fyers_side,      # 1 = buy, -1 = sell
        "productType": "INTRADAY",
        "limitPrice": 0,
        "stopPrice": 0,
        "validity": "DAY",
        "disclosedQty": 0,
        "offlineOrder": False,
        "stopLoss": 0,
        "takeProfit": 0,
        "optType": opt_type,
        "optStrike": str(selection.strike),
    }

    return order_details, f"{selection.notes} | source={source}"


# -------------------- Order placement --------------------

def place_order(fyers_integration, order_details):
    try:
        logger.info(f"Placing order via Fyers: {order_details}")
        fyers = fyers_integration.get_fyers_instance()
        response = fyers.place_order(order_details)
        logger.info(f"Order response: {response}")
    except Exception as e:
        logger.error(f"Order placement error: {e}")


def dispatch_order(fyers_integration, order_details):
    """
    Fire-and-forget wrapper so webhook responses are instant.
    """
    threading.Thread(
        target=lambda: place_order(fyers_integration, order_details),
        daemon=True,
    ).start()
