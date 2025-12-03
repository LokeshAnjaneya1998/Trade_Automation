# src/premarket/services.py

import datetime as dt
from typing import Dict, Optional

import logging
import pandas as pd
import pytz
import requests
from fyers_apiv3 import fyersModel

from .models import (
    NiftyRegime,
    OIPressure,
)

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

    def __init__(
        self,
        fyers_client: fyersModel.FyersModel,
    ):
        self.fyers = fyers_client
        self._ist = IST

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
        resp = self.fyers.history(payload)
        if resp.get("s") != "ok":
            raise RuntimeError(f"Error fetching history for {symbol}: {resp}")

        df = self._to_df_from_history(resp)
        logger.info(f"Fetched {len(df)} daily candles for {symbol}")
        return df.tail(days)

    def _fetch_ltp(self, symbol: str) -> float:
        resp = self.fyers.quotes({"symbols": symbol})
        if resp.get("s") != "ok":
            raise RuntimeError(f"Error fetching quotes for {symbol}: {resp}")

        data = resp.get("d", [])
        if not data:
            raise RuntimeError(f"No quote data for {symbol}")

        v = data[0].get("v", {})
        if "lp" not in v:
            raise RuntimeError(f"No last price in quote: {v}")

        return float(v["lp"])

    def _fetch_quote_change_pct(self, symbol: str) -> float:
        """Fetch percentage change for a symbol if available."""
        resp = self.fyers.quotes({"symbols": symbol})
        if resp.get("s") != "ok":
            raise RuntimeError(f"Error fetching quotes for {symbol}: {resp}")

        data = resp.get("d", [])
        if not data:
            raise RuntimeError(f"No quote data for {symbol}")

        v = data[0].get("v", {})
        if "chp" in v:
            return float(v["chp"])

        # Fallback: compute from last price and previous close if present
        lp = v.get("lp")
        prev_close = v.get("prev_close") or v.get("prevClose")
        if lp is not None and prev_close not in (None, 0):
            return (float(lp) - float(prev_close)) / float(prev_close) * 100.0

        raise RuntimeError(f"No change% data for {symbol}: {v}")

    # ---------- Public functions ----------

    def fetch_india_vix(self) -> float:
        df = self._fetch_daily_history(self.vix_symbol, days=2)
        return self._nice(df["Close"].iloc[-1])

    def classify_nifty_regime(self) -> NiftyRegime:
        df = self._fetch_daily_history(self.nifty_symbol, days=40)

        atr = self._compute_atr(df, period=20)
        atr_20 = float(atr.iloc[-1])

        prev = df.iloc[-1]
        prev_range = float(prev["High"] - prev["Low"])

        if prev_range > 1.5 * atr_20:
            vol = "High Volatility (range > 1.5x ATR20)"
        elif prev_range < 0.8 * atr_20:
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
        implied_open = self._fetch_ltp(self.nifty_symbol)

        gap_points = implied_open - last_close
        gap_mult = gap_points / atr_20

        if abs(gap_mult) < 0.3:
            gap_type = "Small gap (<0.3x ATR)"
        elif abs(gap_mult) < 1.0:
            gap_type = "Medium gap (0.3-1x ATR)"
        else:
            gap_type = "Large gap (>1x ATR)"

        logger.info(
            f"NIFTY regime: atr_20={atr_20:.2f}, prev_range={prev_range:.2f}, "
            f"vol='{vol}', trend='{trend}', gap_points={gap_points:.2f}, gap_mult={gap_mult:.2f}, gap_type='{gap_type}'"
        )

        return NiftyRegime(
            atr_20=self._nice(atr_20),
            day_range_prev=self._nice(prev_range),
            vol_regime=vol,
            trend_regime=trend,
            gap_points=self._nice(gap_points),
            gap_multiple_atr=self._nice(gap_mult),
            gap_type=gap_type,
        )



# ============================================================
# 2) Option Chain Service (NSE API)
# ============================================================

class OptionChainService:
    """
    pull NSE option chain for OI skew
    """

    NSE_BASE = "https://www.nseindia.com"
    HEADERS = {
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "application/json, text/plain, */*",
    }

    def __init__(self, symbol: str = "NIFTY"):
        self.symbol = symbol

    def _fetch_raw(self):
        session = requests.Session()
        session.headers.update(self.HEADERS)
        _ = session.get(self.NSE_BASE, timeout=10)

        url = f"{self.NSE_BASE}/api/option-chain-indices?symbol={self.symbol}"
        resp = session.get(url, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def compute_oi_pressure(self) -> Optional[OIPressure]:
        try:
            data = self._fetch_raw()
        except Exception:
            logger.warning("Option chain fetch failed; returning None")
            return None

        underlying = data.get("records", {}).get("underlyingValue")
        if not underlying:
            logger.warning("Option chain missing underlying value; returning None")
            return None

        underlying = float(underlying)
        records = data.get("records", {}).get("data", [])

        ce_oi = 0
        pe_oi = 0

        for row in records:
            strike = row.get("strikePrice")
            if strike is None:
                continue
            if abs(float(strike) - underlying) <= 200:
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
            f"OI pressure: spot={underlying}, ce_oi_near={ce_oi}, pe_oi_near={pe_oi}, pressure={skew}"
        )

        return OIPressure(
            spot=underlying,
            ce_oi_near=ce_oi,
            pe_oi_near=pe_oi,
            pressure=skew,
        )
