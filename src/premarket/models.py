# src/premarket/models.py

from dataclasses import dataclass
from typing import Optional, List, Dict, Any


@dataclass
class NiftyRegime:
    atr_20: float
    day_range_prev: float
    vol_regime: str
    trend_regime: str
    gap_points: float
    gap_multiple_atr: float
    gap_type: str


@dataclass
class OIPressure:
    spot: float
    ce_oi_near: int
    pe_oi_near: int
    pressure: str


@dataclass
class PremarketSummary:
    as_of_ist: str
    nifty_regime: NiftyRegime
    india_vix: float
    oi_pressure: Optional[OIPressure]


@dataclass
class Checkpoint:
    title: str
    status: str   # "PASS" | "WARN" | "FAIL" | "INFO"
    message: str


@dataclass
class Suggestion:
    title: str
    detail: str


@dataclass
class PremarketSummaryResponse:
    summary: Dict[str, Any]
    checkpoints: List[Checkpoint]
    suggestions: List[Suggestion]