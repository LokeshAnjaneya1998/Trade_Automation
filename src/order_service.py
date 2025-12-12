import logging
import os
import threading
import time
from datetime import datetime, date, time as dt_time
from collections import deque
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
# Share the same cache object as the OptionChainService class, so premarket and
# order paths read/write a single source of truth.
OPTION_CHAIN_CACHE = option_chain_service.CACHE
_OPTION_CHAIN_LOCK = threading.Lock()
_OPTION_CHAIN_INFLIGHT = False
_OPTION_CHAIN_LAST_ERROR_TS = 0.0
_OPTION_CHAIN_HAD_ERROR = False
LAST_ORDER = {}
LAST_ORDERS = deque(maxlen=5)
PROFILE_CACHE = {"ts": 0.0, "data": None}


# -------------------- Strike helpers --------------------

def round_to_nearest_strike(spot_price: float, step: int = 50) -> int:
    """
    Round the given price to the nearest valid strike (NIFTY uses 50-pt steps).
    Example: 22437.5 -> 22450.
    """
    if step <= 0:
        raise ValueError("step must be > 0")
    return int(round(spot_price / step) * step)


def _is_expiry_trading_day(now_ist: datetime, *, calendar, expiry_weekday: int = 1, after_close: dt_time = dt_time(15, 30)) -> bool:
    """
    True if 'now_ist.date()' is the active weekly expiry trading day for NIFTY.
    """
    expiry_d = fyers_expiry_for_now(now_ist, calendar=calendar, expiry_weekday=expiry_weekday, after_close=after_close)
    return calendar.is_trading_day(now_ist.date()) and now_ist.date() == expiry_d


def fyers_expiry_for_now(now_ist: datetime, *, calendar, expiry_weekday: int = 1, after_close: dt_time = dt_time(15, 30)) -> date:
    """
    Helper to reuse expiry computation in one place.
    """
    from src.expiry_utils import next_weekly_expiry_date

    return next_weekly_expiry_date(
        now_ist,
        calendar=calendar,
        expiry_weekday=expiry_weekday,
        after_close=after_close,
    )


def select_strike(
    *,
    spot_price: float,
    opt_type: str,
    signal_time: datetime,
    is_expiry_day: bool,
    iv_percent: float | None = None,
    momentum_flag: bool | None = None,
) -> int:
    """
    Select NIFTY strike based on time-of-day, expiry bias, IV and momentum.

    Tiers:
      - <11:00 -> ATM
      - 11:00-13:00 -> ATM (upgrade to ITM if strong momentum or high IV)
      - 13:00-14:30 -> ITM
      - >14:30 -> Deep ITM
    Expiry bias:
      - After 13:00 on expiry -> at least ITM
      - After 14:30 on expiry -> Deep ITM always
    IV bias (if available):
      - IV >= 15 -> upgrade one tier unless early and strong momentum
      - IV <= 12 -> keep baseline
    """
    if opt_type not in {"CE", "PE"}:
        raise ValueError("opt_type must be 'CE' or 'PE'")
    if spot_price <= 0:
        raise ValueError("spot_price must be > 0")

    atm = round_to_nearest_strike(spot_price, 50)
    if opt_type == "CE":
        itm = atm - 50
        deep_itm = atm - 100
    else:
        itm = atm + 50
        deep_itm = atm + 100

    # keep strikes sensible
    lower_bound = max(0, atm - 800)
    upper_bound = atm + 800

    def clamp(x: int) -> int:
        return max(lower_bound, min(upper_bound, x))

    t = signal_time.time()
    tier = "ATM"
    if t >= dt_time(14, 30):
        tier = "DEEP_ITM"
    elif t >= dt_time(13, 0):
        tier = "ITM"
    elif t >= dt_time(11, 0):
        tier = "ATM_OR_ITM"
    else:
        tier = "ATM"

    if is_expiry_day:
        if t >= dt_time(14, 30):
            tier = "DEEP_ITM"
        elif t >= dt_time(13, 0):
            tier = "ITM"

    # IV and momentum nudges
    if iv_percent is not None and iv_percent >= 15:
        if not (t < dt_time(11, 0) and momentum_flag):
            if tier == "ATM":
                tier = "ITM"
            elif tier == "ATM_OR_ITM":
                tier = "ITM"
            elif tier == "ITM":
                tier = "DEEP_ITM"

    if momentum_flag and t < dt_time(13, 0) and not is_expiry_day:
        tier = "ATM" if tier in {"ATM_OR_ITM", "ATM"} else tier

    if tier == "DEEP_ITM":
        strike = deep_itm
    elif tier == "ITM":
        strike = itm
    elif tier == "ATM_OR_ITM":
        strike = itm if (momentum_flag or (iv_percent and iv_percent > 15)) else atm
    else:
        strike = atm

    return clamp(strike)


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
    Fire-and-forget refresh of option chain (via Fyers). Does not block webhook.
    """
    global _OPTION_CHAIN_INFLIGHT, _OPTION_CHAIN_LAST_ERROR_TS, _OPTION_CHAIN_HAD_ERROR
    with _OPTION_CHAIN_LOCK:
        if _OPTION_CHAIN_INFLIGHT:
            return
        _OPTION_CHAIN_INFLIGHT = True

    def _worker():
        global _OPTION_CHAIN_INFLIGHT, _OPTION_CHAIN_LAST_ERROR_TS, _OPTION_CHAIN_HAD_ERROR
        try:
            option_chain_service.refresh_cache()
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
    def _cached_underlying():
        cached = get_cached_option_chain()
        if cached:
            try:
                return float(
                    (cached.get("records", {}) or {}).get("underlyingValue")
                    or (cached.get("filtered", {}).get("data", [{}])[0].get("underlyingValue"))
                )
            except Exception:
                return None
        return None

    cached_val = _cached_underlying()
    if cached_val:
        return cached_val

    # Try live quotes with small retry/backoff; fall back to cached underlying if throttled.
    fyers = fyers_integration.get_fyers_instance()
    last_exc = None
    for delay in (0.0, 0.5, 1.0):
        if delay:
            time.sleep(delay)
        try:
            resp = fyers.quotes({"symbols": "NSE:NIFTY50-INDEX"})
            if resp.get("s") != "ok":
                raise RuntimeError(f"Error fetching NIFTY quote: {resp}")

            data = resp.get("d") or []
            v = data[0].get("v", {}) if data else {}
            lp = v.get("lp")
            if lp is None:
                raise RuntimeError(f"Last price missing in quote payload: {resp}")
            return float(lp)
        except Exception as exc:
            last_exc = exc
            continue

    # fallback to cached underlying if we have it (re-check) or try a forced refresh
    cached_val = _cached_underlying()
    if not cached_val:
        try:
            option_chain_service.refresh_cache()
            cached_val = _cached_underlying()
        except Exception as exc:
            logger.debug("Forced option-chain refresh failed during spot fetch: %s", exc)

    if cached_val:
        logger.warning("Using cached underlying price due to quote failures: %s", last_exc)
        return cached_val

    raise RuntimeError(f"Error fetching NIFTY quote after retries: {last_exc}")


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


def compute_cached_oi_pressure(search_window: float = 200.0):
    """
    Compute a lightweight OI snapshot using the cached option chain data.
    Returns dict with spot, ce_oi_near, pe_oi_near, pressure or None if unavailable.
    """
    oc = option_chain_service.compute_oi_pressure()
    if not oc:
        return None
    return {
        "spot": oc.spot,
        "ce_oi_near": oc.ce_oi_near,
        "pe_oi_near": oc.pe_oi_near,
        "pressure": oc.pressure,
    }


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
    now_ist = datetime.now(IST)
    is_expiry_day = selection.expiry_date == now_ist.date()
    strike_override = select_strike(
        spot_price=spot_price,
        opt_type=opt_type,
        signal_time=now_ist,
        is_expiry_day=is_expiry_day,
        iv_percent=None,
        momentum_flag=None,
    )
    if strike_override != selection.strike:
        selection.strike = strike_override
        selection.symbol = fyers_nifty_option_symbol(
            underlying="NIFTY",
            expiry_d=selection.expiry_date,
            strike=strike_override,
            opt_type=opt_type,
            calendar=TRADING_CALENDAR,
            expiry_weekday=1,
            exchange_prefix="NSE:",
        )
        selection.notes += f" | strike_override={strike_override}"

    fyers_side = 1 if str(side).lower() in {"1", "buy", "b"} else -1

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

    note = f"{selection.notes} | source={source}"
    record_last_order(note, order_details, mode=setup_type)
    return order_details, note


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


# -------------------- Last order tracking --------------------

def record_last_order(note: str, order_details: dict, mode: str = ""):
    entry = {
        "note": note,
        "order_details": order_details,
        "mode": mode,
        "ts": time.time(),
    }
    LAST_ORDER.update(entry)
    LAST_ORDERS.appendleft(entry)


def get_last_order():
    return LAST_ORDER if LAST_ORDER else None


def get_recent_orders():
    return list(LAST_ORDERS)


def get_option_chain_cache_info():
    """
    Returns cache age/status for option chain.
    """
    ts = OPTION_CHAIN_CACHE.get("ts") or 0
    data_present = OPTION_CHAIN_CACHE.get("data") is not None
    age = time.time() - ts if ts else None
    return {
        "has_data": data_present,
        "last_refresh_ts": ts,
        "age_sec": age,
    }


def get_profile_snapshot(fyers_integration):
    """
    Pull lightweight profile/funds/positions/orders summary from Fyers.
    """
    now = time.time()
    if PROFILE_CACHE["data"] and (now - PROFILE_CACHE["ts"]) <= 15:
        return PROFILE_CACHE["data"]

    fy = fyers_integration.get_fyers_instance()
    def _unwrap(resp):
        if not resp:
            return {}
        if isinstance(resp, dict):
            if "data" in resp and isinstance(resp.get("data"), dict):
                return resp.get("data")
            if "d" in resp and isinstance(resp.get("d"), dict):
                return resp.get("d")
        return resp

    def _first_dict(val):
        if isinstance(val, list):
            return val[0] if val else {}
        return val if isinstance(val, dict) else {}

    profile = {}
    funds = {}
    positions = {}
    orders = {}
    try:
        profile = _unwrap(fy.get_profile() or {})
    except Exception as exc:
        logger.debug("Profile fetch failed: %s", exc)
    try:
        funds = _unwrap(fy.funds() or {})
    except Exception as exc:
        logger.debug("Funds fetch failed: %s", exc)
    try:
        positions = _unwrap(fy.positions() or {})
    except Exception as exc:
        logger.debug("Positions fetch failed: %s", exc)
    try:
        orders = _unwrap(fy.orderbook() or {})
    except Exception as exc:
        logger.debug("Orderbook fetch failed: %s", exc)

    fy_id = profile.get("fy_id") or profile.get("id") or profile.get("client_id") or "N/A"
    name = profile.get("name") or profile.get("display_name") or profile.get("clientName") or "N/A"

    # Funds: attempt equity/available balance
    balance = None
    fl = _first_dict(funds.get("fund_limit") or funds.get("fundLimit") or funds)
    balance = (
        fl.get("equityAmount")
        or fl.get("totalBalance")
        or fl.get("AvailableBalance")
        or fl.get("available_balance")
        or fl.get("opening_balance")
    )
    balance = balance if balance is not None else "N/A"

    net_positions = (
        positions.get("netPositions")
        or positions.get("net_positions")
        or positions.get("overall")
        or []
    )
    open_positions = len(net_positions)
    pnl = 0.0
    for p in net_positions:
        try:
            pnl += float(p.get("overallPnl") or p.get("pnl") or p.get("pl") or 0)
        except Exception:
            continue

    ob = orders.get("orderBook") or orders.get("order_book") or []
    open_orders = sum(1 for o in ob if str(o.get("status")) in {"6", "open", "pending"})
    closed_orders = sum(1 for o in ob if str(o.get("status")) in {"2", "completed", "complete", "filled"})

    data = {
        "fy_id": fy_id,
        "name": name,
        "balance": balance,
        "open_orders": open_orders,
        "closed_orders": closed_orders,
        "pnl": pnl,
    }

    PROFILE_CACHE["data"] = data
    PROFILE_CACHE["ts"] = now
    return data
