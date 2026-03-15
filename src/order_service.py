import logging
import os
import threading
import time
from datetime import datetime, date, time as dt_time
from collections import deque
from typing import Tuple

import pytz

from src.calendar_loader import get_nse_trading_calendar_for_current_year
from src.premarket.services import OptionChainService
from src.expiry_utils import fyers_nifty_option_symbol


logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# Shared trading context
TRADING_CALENDAR = get_nse_trading_calendar_for_current_year()
option_chain_service = OptionChainService(symbol="NIFTY")
# Share the same cache object as the OptionChainService instance, so premarket
# and order paths read/write a single source of truth.
OPTION_CHAIN_CACHE = option_chain_service.cache
_OPTION_CHAIN_LOCK = threading.Lock()
_OPTION_CHAIN_INFLIGHT = False
_OPTION_CHAIN_LAST_ERROR_TS = 0.0
_OPTION_CHAIN_HAD_ERROR = False
_MARKET_STATUS_CACHE: dict = {"ts": 0.0, "open": None, "reason": ""}
_MARKET_STATUS_LOCK = threading.Lock()
_MARKET_STATUS_INFLIGHT = False
LAST_ORDER = {}
LAST_ORDERS = deque(maxlen=5)
PROFILE_CACHE = {"ts": 0.0, "data": None}
LAST_SPOT_PRICE: float | None = None
# Track first entry per opt type (CE/PE) per day to reuse same strike for add-ons and exits.
ENTRY_TRACKER: dict[str, dict] = {"CE": None, "PE": None}
_ENTRY_TRACKER_LOCK = threading.Lock()


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
    setup_type: str = "BREAKOUT",
    iv_percent: float | None = None,
    momentum_flag: bool | None = None,
    oi_confirms: bool | None = None,
) -> int:
    """
    Select NIFTY strike based on time-of-day, setup type, and expiry day.

    REVERSAL trades:
      - Always ATM regardless of time or expiry — short counter-moves need
        high delta, not a gamma lottery ticket.

    Expiry day (gamma play, BREAKOUT/ADD_ON only):
      - BREAKOUT -> OTM (single step out of money, max gamma leverage)
      - Other    -> ATM (safe fallback)

    Non-expiry day tiers (time-of-day):
      - <11:00        -> ATM
      - 11:00-13:00   -> ATM (upgrade to ITM on high IV or strong momentum)
      - 13:00-14:30   -> ITM
      - >14:30        -> Deep ITM

    OI modifier (oi_confirms):
      - True  -> OI aligns with direction; no change (already aggressive)
      - False -> OI contradicts direction; shift one step toward ATM/ITM
      - None  -> Balanced OI or unavailable; no change

    IV nudge (if provided):
      - IV >= 15 -> upgrade one tier (applied before OI modifier)
    """
    if opt_type not in {"CE", "PE"}:
        raise ValueError("opt_type must be 'CE' or 'PE'")
    if spot_price <= 0:
        raise ValueError("spot_price must be > 0")

    atm = round_to_nearest_strike(spot_price, 50)
    if opt_type == "CE":
        otm      = atm + 50
        itm      = atm - 50
        deep_itm = atm - 100
    else:
        otm      = atm - 50
        itm      = atm + 50
        deep_itm = atm + 100

    lower_bound = max(0, atm - 800)
    upper_bound = atm + 800

    def clamp(x: int) -> int:
        return max(lower_bound, min(upper_bound, x))

    # REVERSAL: always ATM — counter-trend moves are short and sharp;
    # delta matters more than leverage here.
    if setup_type == "REVERSAL":
        return clamp(atm)

    # Expiry day: gamma play — OTM for breakout, ATM otherwise
    if is_expiry_day:
        strike = otm if setup_type == "BREAKOUT" else atm
        # OI modifier still applies on expiry day
        if oi_confirms is False and strike == otm:
            strike = atm  # OTM -> ATM when OI contradicts
        return clamp(strike)

    t = signal_time.time()
    if t >= dt_time(14, 30):
        tier = "DEEP_ITM"
    elif t >= dt_time(13, 0):
        tier = "ITM"
    elif t >= dt_time(11, 0):
        tier = "ATM_OR_ITM"
    else:
        tier = "ATM"

    # IV nudge
    if iv_percent is not None and iv_percent >= 15:
        if not (t < dt_time(11, 0) and momentum_flag):
            if tier == "ATM":
                tier = "ITM"
            elif tier == "ATM_OR_ITM":
                tier = "ITM"
            elif tier == "ITM":
                tier = "DEEP_ITM"

    # Momentum: keep ATM when momentum is strong and it's early
    if momentum_flag and t < dt_time(13, 0):
        tier = "ATM" if tier in {"ATM_OR_ITM", "ATM"} else tier

    if tier == "DEEP_ITM":
        strike = deep_itm
    elif tier == "ITM":
        strike = itm
    elif tier == "ATM_OR_ITM":
        strike = itm if (momentum_flag or (iv_percent and iv_percent > 15)) else atm
    else:
        strike = atm

    # OI modifier: if OI contradicts direction, shift one step toward ATM
    if oi_confirms is False:
        if strike == deep_itm:
            strike = itm
            logger.debug("OI contradicts — strike shifted DEEP_ITM -> ITM")
        elif strike == itm:
            strike = atm
            logger.debug("OI contradicts — strike shifted ITM -> ATM")
        # ATM stays ATM (already most conservative)

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
    global LAST_SPOT_PRICE
    def _cached_underlying():
        cached = get_cached_option_chain(max_age_sec=300)
        if cached:
            try:
                return float(
                    (cached.get("records", {}) or {}).get("underlyingValue")
                    or (cached.get("filtered", {}).get("data", [{}])[0].get("underlyingValue"))
                )
            except Exception:
                return None
        # Fall back to last computed OI snapshot if available
        try:
            if option_chain_service.last_result:
                return float(option_chain_service.last_result.spot)
        except Exception:
            pass
        # Last resort: previously seen spot
        try:
            if LAST_SPOT_PRICE:
                return float(LAST_SPOT_PRICE)
        except Exception:
            pass
        return None

    cached_val = _cached_underlying()
    if cached_val:
        LAST_SPOT_PRICE = cached_val
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
            LAST_SPOT_PRICE = float(lp)
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

    # Give clearer message to webhook caller, avoid 500 with opaque text
    raise RuntimeError(
        f"Error fetching NIFTY quote after retries; no cached underlying available. Last error: {last_exc}"
    )


def _get_expiry_from_chain(calendar) -> date:
    """
    Extract expiry date from cached option chain.
    Falls back to calendar-based computation if chain is unavailable.
    """
    raw = get_cached_option_chain()
    if raw:
        filtered = raw.get("filtered", {}) or {}
        expiry_str = (
            filtered.get("expiryDate")
            or (raw.get("records", {}).get("expiryDates") or [None])[0]
        )
        if expiry_str:
            try:
                return datetime.strptime(expiry_str, "%d-%b-%Y").date()
            except Exception:
                pass
    # Calendar fallback
    return fyers_expiry_for_now(datetime.now(IST), calendar=calendar)


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
    qty: int | None = None,
):
    with _ENTRY_TRACKER_LOCK:
        return _build_order_details_locked(fyers_integration, direction, setup_type, spot_price, side, qty)


def _build_order_details_locked(
    fyers_integration,
    direction: str,
    setup_type: str,
    spot_price: float | None = None,
    side: str = "buy",
    qty: int | None = None,
):
    if spot_price is None:
        spot_price = fetch_nifty_spot_price(fyers_integration)

    opt_type = "CE" if direction == "LONG_CALL" else "PE"
    now_ist = datetime.now(IST)
    today = now_ist.date()

    # ── qty resolution ──────────────────────────────────────────
    if qty is None:
        qty_final = 75
    else:
        try:
            qty_final = int(qty)
        except Exception:
            raise ValueError(f"Invalid qty value: {qty}")
        if qty_final <= 0:
            raise ValueError(f"Quantity must be positive; got {qty_final}")

    fyers_side = 1 if str(side).lower() in {"1", "buy", "b"} else -1

    def _entry_today() -> dict | None:
        rec = ENTRY_TRACKER.get(opt_type)
        return rec if (rec and rec.get("date") == today) else None

    def _record_entry(strike, expiry_d, symbol, qty_used):
        existing = ENTRY_TRACKER.get(opt_type)
        if existing and existing.get("date") == today:
            existing["total_qty"] = existing.get("total_qty", 0) + qty_used
        else:
            ENTRY_TRACKER[opt_type] = {
                "date": today,
                "strike": strike,
                "expiry_date": expiry_d,
                "symbol": symbol,
                "total_qty": qty_used,
            }

    entry_rec = _entry_today()

    # ── EXIT path ───────────────────────────────────────────────
    if setup_type == "EXIT":
        if entry_rec is None:
            raise ValueError(f"No tracked entry for {opt_type} today; cannot exit same strike.")
        exit_qty = entry_rec.get("total_qty", qty_final)
        strike = entry_rec["strike"]
        symbol = entry_rec["symbol"]
        note = (
            f"exit: strike={strike} expiry={entry_rec['expiry_date']} "
            f"qty={exit_qty} | source=entry_tracker"
        )
        order_details = {
            "symbol": symbol,
            "qty": exit_qty,
            "type": 2,
            "side": -1,  # always sell on exit
            "productType": "INTRADAY",
            "limitPrice": 0,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
            "stopLoss": 0,
            "takeProfit": 0,
            "optType": opt_type,
            "optStrike": str(strike),
        }
        record_last_order(note, order_details, mode=setup_type)
        return order_details, note

    # ── ENTRY / ADD-ON path ─────────────────────────────────────
    expiry_d = _get_expiry_from_chain(TRADING_CALENDAR)
    is_expiry_day = (expiry_d == today)

    if entry_rec is not None:
        # Add-on: reuse same strike, accumulate qty
        strike = entry_rec["strike"]
        symbol = entry_rec["symbol"]
        _record_entry(strike, expiry_d, symbol, qty_final)
        note = (
            f"addon: strike={strike} expiry={expiry_d} qty={qty_final} "
            f"total={entry_rec.get('total_qty', qty_final)} | source=entry_tracker_reuse"
        )
    else:
        # Fresh entry: derive OI confirmation for strike aggressiveness
        oi_confirms = None
        oi_skew = "unavailable"
        try:
            oi_snap = compute_cached_oi_pressure()
            if oi_snap:
                pressure = oi_snap.get("pressure", "")
                oi_skew = pressure
                if opt_type == "CE":
                    if "PE-heavy" in pressure:
                        oi_confirms = True   # bullish crowd confirms CE buy
                    elif "CE-heavy" in pressure:
                        oi_confirms = False  # bearish crowd contradicts CE buy
                else:  # PE
                    if "CE-heavy" in pressure:
                        oi_confirms = True   # bearish crowd confirms PE buy
                    elif "PE-heavy" in pressure:
                        oi_confirms = False  # bullish crowd contradicts PE buy
        except Exception as exc:
            logger.debug("OI pressure skipped for strike selection: %s", exc)

        # Single-pipeline strike selection
        strike = select_strike(
            spot_price=spot_price,
            opt_type=opt_type,
            signal_time=now_ist,
            is_expiry_day=is_expiry_day,
            setup_type=setup_type,
            oi_confirms=oi_confirms,
        )
        atm = round_to_nearest_strike(spot_price)
        symbol = fyers_nifty_option_symbol(
            underlying="NIFTY",
            expiry_d=expiry_d,
            strike=strike,
            opt_type=opt_type,
            calendar=TRADING_CALENDAR,
            expiry_weekday=1,
            exchange_prefix="NSE:",
        )
        _record_entry(strike, expiry_d, symbol, qty_final)
        note = (
            f"spot={spot_price:.1f} atm={atm} strike={strike} "
            f"expiry={expiry_d} expiry_day={is_expiry_day} setup={setup_type} "
            f"oi_skew={oi_skew!r} oi_confirms={oi_confirms}"
        )

    order_details = {
        "symbol": symbol,
        "qty": qty_final,
        "type": 2,
        "side": fyers_side,
        "productType": "INTRADAY",
        "limitPrice": 0,
        "stopPrice": 0,
        "validity": "DAY",
        "disclosedQty": 0,
        "offlineOrder": False,
        "stopLoss": 0,
        "takeProfit": 0,
        "optType": opt_type,
        "optStrike": str(strike),
    }
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


def check_market_open(fyers_integration, max_age_sec: int = 60) -> tuple[bool, str]:
    """
    Returns (is_open, status_string) using Fyers market_status().
    Caches result for max_age_sec seconds. Fails open on API errors so
    legitimate orders are never blocked by a status check failure.
    Uses an inflight flag so only one thread makes the API call when cache is stale.
    """
    global _MARKET_STATUS_CACHE, _MARKET_STATUS_INFLIGHT
    now = time.time()
    with _MARKET_STATUS_LOCK:
        if _MARKET_STATUS_CACHE["open"] is not None and (now - _MARKET_STATUS_CACHE["ts"]) < max_age_sec:
            return _MARKET_STATUS_CACHE["open"], _MARKET_STATUS_CACHE["reason"]
        if _MARKET_STATUS_INFLIGHT:
            # Another thread is already fetching — serve stale rather than duplicate the call
            if _MARKET_STATUS_CACHE["open"] is not None:
                return _MARKET_STATUS_CACHE["open"], _MARKET_STATUS_CACHE["reason"]
            # No cache yet; fall through and let this thread also fetch
        else:
            _MARKET_STATUS_INFLIGHT = True

    try:
        fyers = fyers_integration.get_fyers_instance()
        resp = fyers.market_status()
        if resp.get("s") != "ok":
            logger.warning("market_status API returned non-ok: %s", resp)
            with _MARKET_STATUS_LOCK:
                _MARKET_STATUS_INFLIGHT = False
            return True, "api_error"

        statuses = resp.get("marketStatus") or []
        fo_segment = next(
            (s for s in statuses if s.get("exchange") == "NSE" and "Derivative" in s.get("segment", "")),
            None,
        )
        if fo_segment is None:
            logger.warning("NSE Equity Derivatives segment not found in market_status response")
            with _MARKET_STATUS_LOCK:
                _MARKET_STATUS_INFLIGHT = False
            return True, "segment_not_found"

        status_str = fo_segment.get("status", "UNKNOWN").upper()
        is_open = status_str in {"OPEN", "PRE_OPEN"}
        with _MARKET_STATUS_LOCK:
            _MARKET_STATUS_CACHE = {"ts": now, "open": is_open, "reason": status_str}
            _MARKET_STATUS_INFLIGHT = False
        return is_open, status_str

    except Exception as exc:
        with _MARKET_STATUS_LOCK:
            _MARKET_STATUS_INFLIGHT = False
        logger.warning("market_status check failed: %s", exc)
        return True, "check_error"


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
