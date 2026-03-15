import datetime as dt
import logging
import time
from typing import Any, Dict, List

import pytz

from .models import (
    Checkpoint,
    PremarketSummary,
    PremarketSummaryResponse,
    Suggestion,
)
from .services import FyersMarketDataService, OptionChainService
from src.fyers_integration import FyersIntegration

IST = pytz.timezone("Asia/Kolkata")
logger = logging.getLogger(__name__)


class PremarketAnalyzer:
    """
    High-level orchestration for premarket checks.
    Gathers data using services and produces a summary + suggestions.
    """

    CACHE_TTL = 120  # seconds

    def __init__(self, symbol: str = "NIFTY"):
        self.symbol = symbol
        self.fyers_integration = FyersIntegration()
        self.ocs = OptionChainService(symbol)
        self._cache: Dict[str, Any] = {"ts": 0.0, "data": None}

    def _get_market_data_service(self) -> FyersMarketDataService:
        # Always build with a fresh Fyers client so new access tokens are picked up.
        fyers_client = self.fyers_integration.get_fyers_instance()
        return FyersMarketDataService(fyers_client)

    # ---------- Core summary ----------

    def build_summary(self) -> PremarketSummary:
        logger.info("PremarketAnalyzer: starting build_summary")
        mds = self._get_market_data_service()
        as_of_ist = dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S %Z")
        nifty_regime = mds.classify_nifty_regime()
        logger.debug(f"PremarketAnalyzer: nifty_regime={nifty_regime}")

        try:
            india_vix = mds.fetch_india_vix()
            logger.debug(f"PremarketAnalyzer: india_vix={india_vix}")
        except Exception as exc:
            logger.warning("India VIX fetch failed: %s", exc)
            india_vix = None

        try:
            oi_pressure = self.ocs.compute_oi_pressure()
            logger.debug(f"PremarketAnalyzer: oi_pressure={oi_pressure}")
        except Exception as exc:
            logger.warning("OI pressure fetch failed: %s", exc)
            oi_pressure = None

        return PremarketSummary(
            as_of_ist=as_of_ist,
            nifty_regime=nifty_regime,
            india_vix=india_vix,
            oi_pressure=oi_pressure,
        )

    def _summary_to_dict(self, summary: PremarketSummary) -> Dict[str, Any]:
        nr = summary.nifty_regime
        oi = summary.oi_pressure

        base = {
            "as_of_ist": summary.as_of_ist,
            "nifty_regime": {
                "atr_20": nr.atr_20,
                "day_range_prev": nr.day_range_prev,
                "vol_regime": nr.vol_regime,
                "trend_regime": nr.trend_regime,
                "gap_points": nr.gap_points,
                "gap_multiple_atr": nr.gap_multiple_atr,
                "gap_type": nr.gap_type,
            },
            "india_vix": summary.india_vix,
            "oi_pressure": None,
        }

        if oi:
            base["oi_pressure"] = {
                "spot": oi.spot,
                "ce_oi_near": oi.ce_oi_near,
                "pe_oi_near": oi.pe_oi_near,
                "pressure": oi.pressure,
            }

        return base

    # ---------- Checkpoints ----------

    def _build_checkpoints(self, summary: PremarketSummary) -> List[Checkpoint]:
        nr = summary.nifty_regime
        oi = summary.oi_pressure

        checkpoints: List[Checkpoint] = []

        # Trend
        if "Uptrend" in nr.trend_regime:
            checkpoints.append(Checkpoint(
                title="Trend Direction",
                status="PASS",
                message="Uptrend detected (EMA20 > EMA50). Prefer long ORB setups."
            ))
        elif "Downtrend" in nr.trend_regime:
            checkpoints.append(Checkpoint(
                title="Trend Direction",
                status="PASS",
                message="Downtrend detected (EMA20 < EMA50). Prefer short ORB setups."
            ))
        else:
            checkpoints.append(Checkpoint(
                title="Trend Direction",
                status="WARN",
                message="Sideways EMAs - be selective; no clear HTF trend."
            ))

        # Volatility
        if "High Volatility" in nr.vol_regime:
            checkpoints.append(Checkpoint(
                title="Volatility Regime",
                status="INFO",
                message="High volatility - ORB breakouts may work well but manage risk."
            ))
        elif "Low Volatility" in nr.vol_regime:
            checkpoints.append(Checkpoint(
                title="Volatility Regime",
                status="WARN",
                message="Low volatility - more fake breakouts; prefer retests or smaller size."
            ))
        else:
            checkpoints.append(Checkpoint(
                title="Volatility Regime",
                status="PASS",
                message="Normal volatility - ORB behaviour likely stable."
            ))

        # Gap
        gap_abs = abs(nr.gap_multiple_atr)
        if gap_abs > 1.0:
            checkpoints.append(Checkpoint(
                title="Gap Size",
                status="WARN",
                message=f"Large gap ({nr.gap_multiple_atr}x ATR). Avoid first 5m breakout; wait for retest."
            ))
        elif gap_abs > 0.3:
            checkpoints.append(Checkpoint(
                title="Gap Size",
                status="INFO",
                message=f"Medium gap ({nr.gap_multiple_atr}x ATR). Trade in gap direction only."
            ))
        else:
            checkpoints.append(Checkpoint(
                title="Gap Size",
                status="PASS",
                message="Small gap - standard ORB rules applicable."
            ))

        # OI skew
        if oi:
            if "PE-heavy" in oi.pressure:
                checkpoints.append(Checkpoint(
                    title="OI Skew",
                    status="PASS",
                    message=f"{oi.pressure} - bullish bias, supports long ORB trades."
                ))
            elif "CE-heavy" in oi.pressure:
                checkpoints.append(Checkpoint(
                    title="OI Skew",
                    status="PASS",
                    message=f"{oi.pressure} - bearish bias, supports short ORB trades."
                ))
            else:
                checkpoints.append(Checkpoint(
                    title="OI Skew",
                    status="INFO",
                    message=f"{oi.pressure} - no strong directional clue from OI."
                ))
        else:
            checkpoints.append(Checkpoint(
                title="OI Skew",
                status="WARN",
                message="Unable to fetch OI. Do NOT rely only on ORB; confirm with price action."
            ))

        return checkpoints

    # ---------- Suggestions ----------

    def _build_suggestions(self, summary: PremarketSummary) -> List[Suggestion]:
        nr = summary.nifty_regime
        oi = summary.oi_pressure

        suggestions: List[Suggestion] = []

        # Side bias
        if "Uptrend" in nr.trend_regime and oi and "PE-heavy" in oi.pressure:
            suggestions.append(Suggestion(
                title="Side Bias",
                detail="Environment supports LONG bias: uptrend + PE-heavy OI."
            ))
        elif "Downtrend" in nr.trend_regime and oi and "CE-heavy" in oi.pressure:
            suggestions.append(Suggestion(
                title="Side Bias",
                detail="Environment supports SHORT bias: downtrend + CE-heavy OI."
            ))
        else:
            suggestions.append(Suggestion(
                title="Side Bias",
                detail="Mixed bias. Reduce position size and demand very clean ORB setups."
            ))

        # ORB style
        if "Low Volatility" in nr.vol_regime:
            suggestions.append(Suggestion(
                title="ORB Style",
                detail="Use ORB retests instead of pure breakouts. Avoid chasing weak candles."
            ))
        elif "High Volatility" in nr.vol_regime:
            suggestions.append(Suggestion(
                title="ORB Style",
                detail="ORB breakouts can run strongly. Trail stops using ATR and let winners expand."
            ))

        # Gap handling
        gap_abs = abs(nr.gap_multiple_atr)
        if gap_abs > 1.0:
            suggestions.append(Suggestion(
                title="Gap Handling",
                detail="Large gap day: turn OFF first 5m ORB entries. Only enable trades after a retest."
            ))

        return suggestions

    # ---------- Public API ----------

    def analyze(self) -> PremarketSummaryResponse:
        logger.info("PremarketAnalyzer: running analyze()")
        summary = self.build_summary()
        checkpoints = self._build_checkpoints(summary)
        suggestions = self._build_suggestions(summary)
        summary_dict = self._summary_to_dict(summary)
        logger.debug(f"PremarketAnalyzer: summary_dict={summary_dict}")
        logger.debug(f"PremarketAnalyzer: checkpoints={checkpoints}")
        logger.debug(f"PremarketAnalyzer: suggestions={suggestions}")

        return PremarketSummaryResponse(
            summary=summary_dict,
            checkpoints=checkpoints,
            suggestions=suggestions,
        )

    def analyze_as_dict(self) -> Dict[str, Any]:
        """
        JSON-serializable structure for Flask jsonify() and frontend JS.
        """
        now = time.time()
        cache_age = now - self._cache["ts"]
        if self._cache["data"] and cache_age < self.CACHE_TTL:
            return self._cache["data"]

        try:
            resp = self.analyze()
            data = {
                "summary": resp.summary,
                "checkpoints": [
                    {"title": c.title, "status": c.status, "message": c.message}
                    for c in resp.checkpoints
                ],
                "suggestions": [
                    {"title": s.title, "detail": s.detail}
                    for s in resp.suggestions
                ],
            }
            self._cache["data"] = data
            self._cache["ts"] = now
            return data
        except Exception as exc:
            # If we have a recent snapshot, serve it instead of failing the request.
            if self._cache["data"]:
                logger.warning(f"Premarket analyze failed, serving cached snapshot: {exc}")
                return self._cache["data"]
            raise
