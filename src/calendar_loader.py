# src/calendar_loader.py

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Any, List

from src.expiry_utils import TradingCalendar

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parent / "data" / "holidays"


def _cache_path_for_year(year: int) -> Path:
    return CACHE_DIR / f"holiday_{year}.json"


def _load_raw_from_cache(year: int) -> Dict[str, Any] | None:
    path = _cache_path_for_year(year)
    if not path.exists():
        logger.debug("Holiday cache not found for %d at %s", year, path)
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        logger.info("Loaded holiday cache for %d from %s", year, path)
        return raw
    except Exception as exc:
        logger.warning("Failed to read holiday cache %s: %s", path, exc)
        return None


def _parse_fo_holidays_for_year(raw_json: Dict[str, Any], year: int) -> List[date]:
    fo_entries = raw_json.get("FO", [])
    holidays: List[date] = []
    for row in fo_entries:
        dt_str = row.get("tradingDate")
        if not dt_str:
            continue
        try:
            dt = datetime.strptime(dt_str, "%d-%b-%Y").date()
        except ValueError:
            logger.warning("Unexpected tradingDate format: %r", dt_str)
            continue
        if dt.year == year:
            holidays.append(dt)
    return sorted(set(holidays))


def load_trading_holidays(year: int | None = None) -> List[date]:
    """
    Reads FO holidays from local cache JSON only — no network calls.
    If the file is missing, returns an empty list with a warning.
    Runtime market-open guard is done via Fyers market_status().
    Update the JSON file once a year manually (or copy from NSE website).
    """
    if year is None:
        year = date.today().year
    raw = _load_raw_from_cache(year)
    if raw is None:
        logger.warning(
            "Holiday cache missing for %d — expiry calculation will assume no holidays. "
            "Update %s manually once a year.",
            year,
            _cache_path_for_year(year),
        )
        return []
    holidays = _parse_fo_holidays_for_year(raw, year)
    logger.info("Parsed %d holidays for year %d", len(holidays), year)
    return holidays


def get_trading_calendar(year: int | None = None) -> TradingCalendar:
    if year is None:
        year = date.today().year
    return TradingCalendar(holidays=set(load_trading_holidays(year)))


# Backward-compat alias used in order_service.py
def get_nse_trading_calendar_for_current_year() -> TradingCalendar:
    return get_trading_calendar()
