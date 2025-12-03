# src/expiry_utils.py

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Set, Literal
import logging

# Month code tables
MONTH_LETTERS = ["J", "F", "M", "A", "M", "J", "J", "A", "S", "O", "N", "D"]
MONTH_3L      = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]

TradingCalendarKind = Literal["EQUITY_DERIVATIVES"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TradingCalendar:
    """
    Holds NSE trading holidays & weekends. Used for expiry logic.
    """
    holidays: Set[date]
    weekend_days: Set[int] = frozenset({5, 6})  # Saturday=5, Sunday=6

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() not in self.weekend_days and d not in self.holidays

    def previous_trading_day(self, d: date) -> date:
        cur = d
        while not self.is_trading_day(cur):
            cur -= timedelta(days=1)
        return cur

    def next_trading_day(self, d: date) -> date:
        cur = d
        while not self.is_trading_day(cur):
            cur += timedelta(days=1)
        return cur


def _expiry_search_base(now_ist: datetime, after_close: time) -> date:
    """
    If time is after close (e.g. 15:30 IST), we treat 'base' as next calendar day.
    """
    base = now_ist.date()
    if now_ist.time() >= after_close:
        base = base + timedelta(days=1)
    return base


def next_weekly_expiry_date(
    now_ist: datetime,
    *,
    calendar: TradingCalendar,
    expiry_weekday: int = 1,    # Tuesday (Mon=0)
    after_close: time = time(15, 30),
) -> date:
    """
    Get the actual TRADING DATE for next weekly expiry.

    Steps:
      - Find next calendar Tuesday (or today if Tuesday and before close).
      - If that day is holiday/weekend, move backward to previous trading day.
    """
    base_date = _expiry_search_base(now_ist, after_close)

    # Next Tuesday
    days_ahead = (expiry_weekday - base_date.weekday() + 7) % 7
    if days_ahead == 0:
        candidate = base_date
    else:
        candidate = base_date + timedelta(days=days_ahead)

    expiry_date = calendar.previous_trading_day(candidate)
    logger.debug(
        "next_weekly_expiry_date: now=%s base=%s candidate=%s expiry=%s",
        now_ist.isoformat(),
        base_date,
        candidate,
        expiry_date,
    )
    return expiry_date


def all_month_expiries_for_underlying(
    year: int,
    month: int,
    *,
    calendar: TradingCalendar,
    expiry_weekday: int = 1,  # Tuesday
) -> list[date]:
    """
    For a given month, find all TRADING DAYS that act as weekly expiries.
    """
    from calendar import monthrange

    num_days = monthrange(year, month)[1]
    expiries: Set[date] = set()

    for day in range(1, num_days + 1):
        d = date(year, month, day)
        if d.weekday() == expiry_weekday:
            exp = calendar.previous_trading_day(d)
            expiries.add(exp)

    return sorted(expiries)


def is_monthly_expiry(
    expiry_d: date,
    *,
    calendar: TradingCalendar,
    expiry_weekday: int = 1,
) -> bool:
    """
    True if 'expiry_d' is the LAST weekly expiry trading day of that month.
    """
    month_expiries = all_month_expiries_for_underlying(
        expiry_d.year,
        expiry_d.month,
        calendar=calendar,
        expiry_weekday=expiry_weekday,
    )
    return bool(month_expiries) and expiry_d == month_expiries[-1]


def format_weekly_expiry_code(expiry_d: date) -> str:
    """
    Weekly:
      YY M dd  -> 25D02 for 2025-Dec-02
    """
    yy = expiry_d.year % 100
    mm = expiry_d.month
    dd = expiry_d.day
    m_letter = MONTH_LETTERS[mm - 1]
    return f"{yy:02d}{m_letter}{dd:02d}"


def format_monthly_expiry_code(expiry_d: date) -> str:
    """
    Monthly:
      YY MMM  -> 25DEC for Dec-2025
    """
    yy = expiry_d.year % 100
    mm = expiry_d.month
    m3 = MONTH_3L[mm - 1]
    return f"{yy:02d}{m3}"


def fyers_nifty_option_symbol(
    *,
    underlying: str,       # "NIFTY"
    expiry_d: date,        # actual trading expiry date
    strike: int,
    opt_type: str,         # "CE" or "PE"
    calendar: TradingCalendar,
    expiry_weekday: int = 1,
    exchange_prefix: str = "NSE:",
) -> str:
    """
    Build full option symbol:

    - Weekly (regular Tuesday): NSE:NIFTY25D0226000CE
    - Monthly / last expiry of month: NSE:NIFTY25DEC26000CE
    """
    if is_monthly_expiry(expiry_d, calendar=calendar, expiry_weekday=expiry_weekday):
        expiry_code = format_monthly_expiry_code(expiry_d)
    else:
        expiry_code = format_weekly_expiry_code(expiry_d)

    return f"{exchange_prefix}{underlying}{expiry_code}{strike}{opt_type}"
