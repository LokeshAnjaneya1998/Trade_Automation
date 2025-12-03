# src/calendar_loader.py

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Any, List, Set

import requests

from src.expiry_utils import TradingCalendar

logger = logging.getLogger(__name__)

BASE_URL = "https://www.nseindia.com"
HOLIDAY_API_URL = "https://www.nseindia.com/api/holiday-master?type=trading"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.nseindia.com/",
    "Connection": "keep-alive",
}

CACHE_DIR = Path(__file__).resolve().parent / "data" / "holidays"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


class NseHolidayFetchError(Exception):
    pass


def _get_session() -> requests.Session:
    """
    Session + one warmup GET to main NSE page to avoid 403.
    """
    s = requests.Session()
    s.get(BASE_URL, headers=DEFAULT_HEADERS, timeout=15)
    return s


def _download_raw_holiday_json() -> Dict[str, Any]:
    """
    Download holiday-master JSON from NSE (all segments).
    """
    try:
        session = _get_session()
        resp = session.get(HOLIDAY_API_URL, headers=DEFAULT_HEADERS, timeout=20)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.exception("Failed to fetch NSE holiday-master API")
        raise NseHolidayFetchError(str(exc)) from exc


def _parse_fo_holidays_for_year(raw_json: Dict[str, Any], year: int) -> List[date]:
    """
    Extract FO (Equity Derivatives) holidays for given year as list[date].
    """
    fo_entries = raw_json.get("FO", [])
    holidays: List[date] = []

    for row in fo_entries:
        dt_str = row.get("tradingDate")  # e.g. "26-Feb-2025"
        if not dt_str:
            continue
        try:
            dt = datetime.strptime(dt_str, "%d-%b-%Y").date()
        except ValueError:
            logger.warning("Unexpected tradingDate format: %r", dt_str)
            continue

        if dt.year == year:
            holidays.append(dt)

    holidays = sorted(set(holidays))
    return holidays


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
        logger.info("Loaded NSE holiday cache for %d from %s", year, path)
        return raw
    except Exception as exc:
        logger.warning("Failed to read holiday cache %s: %s", path, exc)
        return None


def _save_raw_to_cache(year: int, raw_json: Dict[str, Any]) -> None:
    path = _cache_path_for_year(year)
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump(raw_json, f, indent=2, sort_keys=True)
        logger.info("Saved NSE holiday JSON cache for %d -> %s", year, path)
    except Exception as exc:
        logger.warning("Failed to write holiday cache %s: %s", path, exc)


def load_or_fetch_nse_trading_holidays(year: int | None = None) -> List[date]:
    """
    Returns list of FO holidays for given year.
    - Tries cache.
    - If not found, fetches from NSE, saves to cache, then parses.
    """
    if year is None:
        year = date.today().year

    raw = _load_raw_from_cache(year)
    if raw is None:
        logger.info("Fetching NSE holiday master for year %d", year)
        raw = _download_raw_holiday_json()

        # Determine which years are present in FO segment
        fo_entries = raw.get("FO", [])
        years_in_json: Set[int] = set()
        for row in fo_entries:
            dt_str = row.get("tradingDate")
            if not dt_str:
                continue
            try:
                dt = datetime.strptime(dt_str, "%d-%b-%Y").date()
                years_in_json.add(dt.year)
            except ValueError:
                continue

        for y in years_in_json:
            _save_raw_to_cache(y, raw)

        if year not in years_in_json:
            raise NseHolidayFetchError(
                f"NSE data does not contain FO holidays for year={year}. "
                f"Years present: {sorted(years_in_json)}"
            )

    holidays = _parse_fo_holidays_for_year(raw, year)
    logger.info("Parsed %d NSE holidays for year %d", len(holidays), year)
    return holidays


def get_nse_trading_calendar_for_year(year: int | None = None) -> TradingCalendar:
    """
    Return TradingCalendar for given year.
    """
    if year is None:
        year = date.today().year
    holidays = load_or_fetch_nse_trading_holidays(year)
    return TradingCalendar(holidays=set(holidays))


def get_nse_trading_calendar_for_current_year() -> TradingCalendar:
    """
    Convenience: use current year.
    When year changes, this will fetch new holidays for the new year.
    """
    return get_nse_trading_calendar_for_year(date.today().year)
