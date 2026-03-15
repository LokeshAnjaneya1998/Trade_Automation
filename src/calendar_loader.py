# src/calendar_loader.py

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Any, List

from src.expiry_utils import TradingCalendar

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parent / "data" / "holidays"
CACHE_FILE = CACHE_DIR / "holidays.json"
NSE_HOLIDAY_URL = "https://www.nseindia.com/api/holiday-master?type=trading"
_NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}


def _load_all_cache() -> Dict[str, Any]:
    """Load the entire holidays.json file; returns {} if missing or corrupt."""
    if not CACHE_FILE.exists():
        return {}
    try:
        with CACHE_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning("Failed to read holiday cache %s: %s", CACHE_FILE, exc)
        return {}


def _save_all_cache(store: Dict[str, Any]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with CACHE_FILE.open("w", encoding="utf-8") as f:
        json.dump(store, f, indent=2)


def _load_raw_from_cache(year: int) -> Dict[str, Any] | None:
    store = _load_all_cache()
    raw = store.get(str(year))
    if raw is None:
        logger.debug("Holiday cache not found for %d in %s", year, CACHE_FILE)
    else:
        logger.info("Loaded holiday cache for %d from %s", year, CACHE_FILE)
    return raw


def _fetch_from_nse(year: int) -> Dict[str, Any] | None:
    """
    Fetch holiday list from NSE API and save to cache.
    NSE returns current year data only — called automatically when cache is missing/stale.
    """
    try:
        import requests
    except ImportError:
        logger.warning("requests not installed — cannot auto-fetch NSE holidays")
        return None

    try:
        session = requests.Session()
        # Seed cookies by hitting the main page first
        session.get("https://www.nseindia.com", headers=_NSE_HEADERS, timeout=10)
        time.sleep(0.5)
        resp = session.get(NSE_HOLIDAY_URL, headers=_NSE_HEADERS, timeout=10)
        resp.raise_for_status()
        raw = resp.json()

        # Validate it has at least one known segment
        if not any(k in raw for k in ("FO", "CM", "CBM")):
            logger.warning("NSE holiday response missing expected keys: %s", list(raw.keys()))
            return None

        # Merge into single holidays.json keyed by year
        store = _load_all_cache()
        store[str(year)] = raw
        _save_all_cache(store)
        logger.info("NSE holidays fetched and saved to %s (year=%d)", CACHE_FILE, year)
        return raw

    except Exception as exc:
        logger.warning("Failed to fetch NSE holidays: %s", exc)
        return None


def _parse_fo_holidays_for_year(raw_json: Dict[str, Any], year: int) -> List[date]:
    # FO = Futures & Options segment; fall back to CM (equity) if FO missing
    fo_entries = raw_json.get("FO") or raw_json.get("CM") or []
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
    Load FO holidays for the given year.
    1. Try local cache file first.
    2. If missing or has no entries for the year, auto-fetch from NSE API.
    3. If fetch fails, assume no holidays (fail-safe).
    """
    if year is None:
        year = date.today().year

    raw = _load_raw_from_cache(year)

    # Check if cached file actually has data for this year
    if raw is not None:
        holidays = _parse_fo_holidays_for_year(raw, year)
        if holidays:
            logger.info("Parsed %d holidays for year %d", len(holidays), year)
            return holidays
        logger.info(
            "Holiday cache for %d exists but has no entries for that year — re-fetching from NSE", year
        )

    # Auto-fetch from NSE
    logger.info("Fetching holiday calendar for %d from NSE API...", year)
    raw = _fetch_from_nse(year)
    if raw is not None:
        holidays = _parse_fo_holidays_for_year(raw, year)
        if holidays:
            logger.info("Parsed %d holidays for year %d from NSE", len(holidays), year)
            return holidays

    logger.warning(
        "Could not load holidays for %d — expiry calculation will assume no holidays.", year
    )
    return []


def get_trading_calendar(year: int | None = None) -> TradingCalendar:
    if year is None:
        today = date.today()
        year = today.year
        holidays = set(load_trading_holidays(year))
        # In December, also load next year's holidays so cross-year expiry
        # calculations work correctly. NSE releases next year's calendar by
        # October/November, so this fetch will succeed when it's actually needed.
        if today.month == 12:
            holidays.update(load_trading_holidays(year + 1))
        return TradingCalendar(holidays=holidays)
    return TradingCalendar(holidays=set(load_trading_holidays(year)))


# Backward-compat alias used in order_service.py
def get_nse_trading_calendar_for_current_year() -> TradingCalendar:
    return get_trading_calendar()
