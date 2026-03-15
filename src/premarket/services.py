# src/premarket/services.py

import datetime as dt
import logging
import time
from typing import Any, Dict, Optional

import pandas as pd
import pytz
from fyers_apiv3 import fyersModel

from .models import NiftyRegime, OIPressure
from src.calendar_loader import get_nse_trading_calendar_for_current_year
from src.expiry_utils import fyers_nifty_option_symbol, next_weekly_expiry_date
from src.fyers_integration import FyersIntegration

IST = pytz.timezone("Asia/Kolkata")
logger = logging.getLogger(__name__)


# ============================================================
# 1) Fyers Market Data Service (replaces yfinance)
# ============================================================

class FyersMarketDataService:
    """
    Market data service that uses official Fyers API.
    Provides:
    - NIFTY daily candles -> ATR20, EMA20/50, Range
    - India VIX daily candles
    - LTP for NIFTY (for gap analysis)
    """

    def __init__(self, fyers_client: fyersModel.FyersModel):
        self.fyers = fyers_client
        self._ist = IST
        self._last_regime: Optional[NiftyRegime] = None

        # Adjust these if your symbols differ in Fyers
        self.nifty_symbol = "NSE:NIFTY50-INDEX"
        self.vix_symbol = "NSE:INDIAVIX-INDEX"

    # ---------- Helpers ----------

    @staticmethod
    def _to_df_from_history(data: Dict) -> pd.DataFrame:
        candles = data.get("candles", [])
        if not candles:
            raise RuntimeError("No candle data from Fyers")

        df = pd.DataFrame(
            candles,
            columns=["timestamp", "Open", "High", "Low", "Close", "Volume"],
        )
        df["timestamp"] = (
            pd.to_datetime(df["timestamp"], unit="s")
            .dt.tz_localize("UTC")
            .dt.tz_convert(IST)
        )
        df.set_index("timestamp", inplace=True)
        return df

    @staticmethod
    def _compute_atr(df: pd.DataFrame, period: int = 20) -> pd.Series:
        high = df["High"]
        low = df["Low"]
        close = df["Close"]
        prev_close = close.shift(1)
        tr = pd.concat(
            [
                high - low,
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        return tr.rolling(period).mean()

    @staticmethod
    def _pct_change(cur: float, prev: float) -> float:
        return 0.0 if prev == 0 else (cur - prev) / prev * 100.0

    @staticmethod
    def _nice(n: float, digits: int = 2) -> float:
        return float(f"{n:.{digits}f}")

    # ---------- Resilient Fyers calls ----------

    def _call_with_retry(self, func, payload: Dict, label: str, retries: int = 2, backoff: float = 0.6) -> Dict:
        """
        Call a Fyers endpoint with basic retry/backoff and better 429 handling.
        """
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = func(payload)
                if not isinstance(resp, dict):
                    raise RuntimeError(f"{label}: non-dict response from Fyers")
                # Handle throttling gracefully
                if resp.get("code") == 429 or (resp.get("s") == "error" and resp.get("code") == 429):
                    raise RuntimeError(f"{label}: Fyers throttled (429)")
                return resp
            except Exception as exc:
                last_exc = exc
                if attempt == retries:
                    break
                time.sleep(backoff)
                backoff *= 2
        raise RuntimeError(f"{label}: failed after retries: {last_exc}") from last_exc

    # ---------- Core Fyers functions ----------

    def _fetch_daily_history(self, symbol: str, days: int = 40) -> pd.DataFrame:
        today = dt.datetime.now(IST).date()
        start = today - dt.timedelta(days=days * 2)

        payload = {
            "symbol": symbol,
            "resolution": "D",
            "date_format": "1",
            "range_from": start.strftime("%Y-%m-%d"),
            "range_to": today.strftime("%Y-%m-%d"),
            "cont_flag": "1",
        }
        resp = self._call_with_retry(self.fyers.history, payload, f"history {symbol}")
        if resp.get("s") != "ok":
            raise RuntimeError(f"Error fetching history for {symbol}: {resp}")

        df = self._to_df_from_history(resp)
        logger.info(f"Fetched {len(df)} daily candles for {symbol}")
        return df.tail(days)

    def _fetch_ltp(self, symbol: str) -> float:
        resp = self._call_with_retry(self.fyers.quotes, {"symbols": symbol}, f"quotes {symbol}")
        if resp.get("s") != "ok":
            raise RuntimeError(f"Error fetching quotes for {symbol}: {resp}")

        data = resp.get("d", [])
        if not data:
            raise RuntimeError(f"No quote data for {symbol}")

        v = data[0].get("v", {})
        if "lp" not in v:
            raise RuntimeError(f"No last price in quote: {v}")

        return float(v["lp"])

    # ---------- Public functions ----------

    def fetch_india_vix(self) -> float:
        df = self._fetch_daily_history(self.vix_symbol, days=2)
        return self._nice(df["Close"].iloc[-1])

    def _fetch_today_range(self) -> float | None:
        """
        Today's intraday high-low range using 5-minute candles.
        Returns None if candles are unavailable (pre-market or API error).
        """
        today = dt.datetime.now(IST).date()
        try:
            payload = {
                "symbol": self.nifty_symbol,
                "resolution": "5",
                "date_format": "1",
                "range_from": today.strftime("%Y-%m-%d"),
                "range_to": today.strftime("%Y-%m-%d"),
                "cont_flag": "1",
            }
            resp = self._call_with_retry(self.fyers.history, payload, "intraday_5m_today")
            if resp.get("s") != "ok":
                return None
            df = self._to_df_from_history(resp)
            if df.empty:
                return None
            return float(df["High"].max() - df["Low"].min())
        except Exception as exc:
            logger.debug("Today's intraday range fetch failed: %s", exc)
            return None

    def classify_nifty_regime(self) -> NiftyRegime:
        try:
            df = self._fetch_daily_history(self.nifty_symbol, days=40)
            now_ist = dt.datetime.now(IST)
            market_open = dt.time(9, 15)

            atr = self._compute_atr(df, period=20)
            atr_20 = float(atr.iloc[-1])

            # #9: Use today's intraday range when market is open; prev-day otherwise
            if now_ist.time() >= market_open:
                vol_range = self._fetch_today_range()
                if vol_range is None:
                    vol_range = float(df.iloc[-1]["High"] - df.iloc[-1]["Low"])
                    logger.debug("Intraday range unavailable; falling back to prev-day range")
            else:
                vol_range = float(df.iloc[-1]["High"] - df.iloc[-1]["Low"])

            if vol_range > 1.5 * atr_20:
                vol = "High Volatility (range > 1.5x ATR20)"
            elif vol_range < 0.8 * atr_20:
                vol = "Low Volatility (range < 0.8x ATR20)"
            else:
                vol = "Normal Volatility"

            close = df["Close"]
            ema20 = close.ewm(span=20).mean()
            ema50 = close.ewm(span=50).mean()

            if ema20.iloc[-1] > ema50.iloc[-1]:
                trend = "Uptrend (EMA20 > EMA50)"
            elif ema20.iloc[-1] < ema50.iloc[-1]:
                trend = "Downtrend (EMA20 < EMA50)"
            else:
                trend = "Sideways / Flat EMAs"

            last_close = float(close.iloc[-1])

            # #10: Gap analysis only pre-market (before 09:15 IST)
            if now_ist.time() < market_open:
                try:
                    implied_open = self._fetch_ltp(self.nifty_symbol)
                    gap_points = implied_open - last_close
                    gap_mult = gap_points / atr_20 if atr_20 else 0.0
                    if abs(gap_mult) < 0.3:
                        gap_type = "Small gap (<0.3x ATR)"
                    elif abs(gap_mult) < 1.0:
                        gap_type = "Medium gap (0.3-1x ATR)"
                    else:
                        gap_type = "Large gap (>1x ATR)"
                except Exception as exc:
                    logger.warning("NIFTY gap calc skipped (quotes error): %s", exc)
                    gap_points, gap_mult, gap_type = 0.0, 0.0, "Gap data unavailable"
            else:
                gap_points, gap_mult, gap_type = 0.0, 0.0, "N/A (market open)"

            regime = NiftyRegime(
                atr_20=self._nice(atr_20),
                day_range_prev=self._nice(vol_range),
                vol_regime=vol,
                trend_regime=trend,
                gap_points=self._nice(gap_points),
                gap_multiple_atr=self._nice(gap_mult),
                gap_type=gap_type,
            )
            self._last_regime = regime
            logger.info(
                "NIFTY regime: atr_20=%.2f, vol_range=%.2f, vol='%s', trend='%s', "
                "gap_points=%.2f, gap_mult=%.2f, gap_type='%s'",
                atr_20, vol_range, vol, trend, gap_points, gap_mult, gap_type,
            )
            return regime
        except Exception as exc:
            logger.warning("NIFTY regime fetch failed: %s", exc)
            if self._last_regime:
                return self._last_regime
            return NiftyRegime(
                atr_20=0.0,
                day_range_prev=0.0,
                vol_regime="Data unavailable",
                trend_regime="Data unavailable",
                gap_points=0.0,
                gap_multiple_atr=0.0,
                gap_type="Data unavailable",
            )


# ============================================================
# 2) Option Chain Service (Fyers-based)
# ============================================================


class OptionChainService:
    """
    Pull NIFTY option chain using Fyers quotes (no NSE scraping).
    Builds a synthetic NSE-like structure and caches it for reuse.
    """

    CACHE_TTL = 20  # seconds
    _CACHE_BY_SYMBOL: Dict[str, Dict[str, Any]] = {}
    _LAST_RESULT_BY_SYMBOL: Dict[str, Optional[OIPressure]] = {}

    def __init__(self, symbol: str = "NIFTY", fyers_integration=None):
        self.symbol = symbol.upper()
        self._cache_key = self.symbol
        self.fyers_integration = fyers_integration or FyersIntegration()
        self.calendar = get_nse_trading_calendar_for_current_year()
        # Keep a per-symbol cache so BANKNIFTY/FINNIFTY instances don't clobber each other.
        self._cache = self._CACHE_BY_SYMBOL.setdefault(self._cache_key, {"ts": 0.0, "data": None})

    @property
    def cache(self) -> Dict[str, any]:
        return self._cache

    @property
    def last_result(self) -> Optional[OIPressure]:
        return self._LAST_RESULT_BY_SYMBOL.get(self._cache_key)

    def _get_fyers(self):
        return self.fyers_integration.get_fyers_instance()

    def _fetch_chain_from_fyers(self, window: int = 200, step: int = 50) -> dict:
        fyers = self._get_fyers()
        logging.getLogger("FyersAPIRequest").setLevel(logging.WARNING)

        # Spot
        spot_resp = fyers.quotes({"symbols": f"NSE:{self.symbol}50-INDEX"})
        if spot_resp.get("s") != "ok":
            raise RuntimeError(f"Fyers quotes failed for spot: {spot_resp}")
        spot_data = (spot_resp.get("d") or [{}])[0].get("v", {})
        spot = float(spot_data.get("lp"))

        # Expiry
        now_ist = dt.datetime.now(IST)
        expiry_d = next_weekly_expiry_date(
            now_ist,
            calendar=self.calendar,
            expiry_weekday=1,
        )
        expiry_str = expiry_d.strftime("%d-%b-%Y")

        # Strikes
        atm = int(round(spot / step) * step)
        strikes = list(range(atm - window, atm + window + step, step))

        symbols = []
        for strike in strikes:
            for opt in ("CE", "PE"):
                symbols.append(
                    fyers_nifty_option_symbol(
                        underlying=self.symbol,
                        expiry_d=expiry_d,
                        strike=strike,
                        opt_type=opt,
                        calendar=self.calendar,
                        expiry_weekday=1,
                        exchange_prefix="NSE:",
                    )
                )

        quotes_resp = fyers.quotes({"symbols": ",".join(symbols)})
        if quotes_resp.get("s") != "ok":
            raise RuntimeError(f"Fyers quotes failed for chain: {quotes_resp}")

        quotes = {item.get("n"): item.get("v", {}) for item in quotes_resp.get("d", [])}

        def _leg(symbol: str):
            v = quotes.get(symbol, {}) or {}
            return {
                "openInterest": v.get("oi") or v.get("open_interest") or 0,
                "totalTradedVolume": v.get("volume") or v.get("vol_traded_today") or 0,
                "lastPrice": v.get("lp") or 0,
            }

        rows = []
        for strike in strikes:
            row = {"strikePrice": strike, "underlyingValue": spot}
            ce_sym = fyers_nifty_option_symbol(
                underlying=self.symbol,
                expiry_d=expiry_d,
                strike=strike,
                opt_type="CE",
                calendar=self.calendar,
                expiry_weekday=1,
                exchange_prefix="NSE:",
            )
            pe_sym = ce_sym.replace("CE", "PE")
            row["CE"] = _leg(ce_sym)
            row["PE"] = _leg(pe_sym)
            rows.append(row)

        payload = {
            "records": {
                "underlyingValue": spot,
                "data": rows,
                "expiryDates": [expiry_str],
            },
            "filtered": {
                "data": rows,
                "expiryDate": expiry_str,
            },
        }
        return payload

    def _fetch_with_cache(self) -> Optional[dict]:
        now = time.time()
        if self._cache["data"] and (now - self._cache["ts"]) <= self.CACHE_TTL:
            return self._cache["data"]
        try:
            raw = self._fetch_chain_from_fyers()
            self._cache["ts"] = now
            self._cache["data"] = raw
            logger.debug("Option chain cache refreshed from Fyers for %s", self.symbol)
            return raw
        except Exception as exc:
            logger.warning("Option chain fetch failed (Fyers) for %s: %s", self.symbol, exc)
            return self._cache["data"]

    def refresh_cache(self):
        """Explicit refresh for background timers."""
        data = self._fetch_chain_from_fyers()
        self._cache["ts"] = time.time()
        self._cache["data"] = data
        logger.debug("Option chain cache refreshed from Fyers for %s", self.symbol)

    def compute_oi_pressure(self) -> Optional[OIPressure]:
        data = self._fetch_with_cache()
        if not data:
            return self.last_result

        records = (data.get("records", {}) or {}).get("data", []) or []
        underlying = data.get("records", {}).get("underlyingValue")
        if underlying is None:
            if self.last_result:
                underlying = self.last_result.spot
            else:
                return self.last_result

        ce_oi = 0
        pe_oi = 0

        for row in records:
            strike = row.get("strikePrice")
            if strike is None:
                continue
            if abs(float(strike) - float(underlying)) <= 200:
                ce = row.get("CE")
                pe = row.get("PE")
                if ce:
                    ce_oi += int(ce.get("openInterest", 0))
                if pe:
                    pe_oi += int(pe.get("openInterest", 0))

        if ce_oi > 1.2 * pe_oi:
            skew = "CE-heavy (bearish skew)"
        elif pe_oi > 1.2 * ce_oi:
            skew = "PE-heavy (bullish skew)"
        else:
            skew = "Balanced OI"

        logger.info(
            f"OI pressure (Fyers): spot={underlying}, ce_oi_near={ce_oi}, pe_oi_near={pe_oi}, pressure={skew}"
        )

        snapshot = OIPressure(
            spot=float(underlying),
            ce_oi_near=ce_oi,
            pe_oi_near=pe_oi,
            pressure=skew,
        )
        self._LAST_RESULT_BY_SYMBOL[self._cache_key] = snapshot
        return snapshot
