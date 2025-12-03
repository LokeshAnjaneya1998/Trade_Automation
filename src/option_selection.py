# src/option_selection.py

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date, time
from typing import Literal

from src.expiry_utils import (
    TradingCalendar,
    next_weekly_expiry_date,
    fyers_nifty_option_symbol,
)

Direction   = Literal["LONG_CALL", "LONG_PUT"]
SetupType   = Literal["BREAKOUT", "REVERSAL"]   # REVERSAL = sideways / ORB bounce
StrikeStyle = Literal["ATM", "OTM_1"]


@dataclass
class OptionSelection:
    direction: Direction
    setup_type: SetupType
    strike_style: StrikeStyle
    strike: int
    expiry_date: date
    symbol: str
    notes: str


def _round_to_step(x: float, step: int) -> int:
    if step <= 0:
        raise ValueError("step must be > 0")
    return int(round(x / step) * step)


def select_nifty_option_for_signal(
    *,
    now_ist: datetime,
    spot_price: float,
    direction: Direction,
    setup_type: SetupType,
    calendar: TradingCalendar,
    expiry_weekday: int = 1,      # Tuesday
    strike_step: int = 50,
    underlying: str = "NIFTY",
    exchange_prefix: str = "NSE:",
    use_otm_on_expiry_breakout: bool = True,
    after_close: time = time(15, 30),
) -> OptionSelection:
    """
    Your rule:

    - BREAKOUT:
        - Non-expiry day  → ATM
        - Expiry day      → 1-OTM (if use_otm_on_expiry_breakout)
    - REVERSAL (sideways / ORB bounce):
        - Always 1-OTM

    Then builds final symbol:
      - Weekly:  NSE:NIFTY25D0226000CE
      - Monthly: NSE:NIFTY25DEC26000CE
    """
    if spot_price <= 0:
        raise ValueError("spot_price must be > 0")

    # 1) Next weekly expiry (trading) date
    expiry_d = next_weekly_expiry_date(
        now_ist,
        calendar=calendar,
        expiry_weekday=expiry_weekday,
        after_close=after_close,
    )

    # 2) Is today that expiry trading day?
    today = now_ist.date()
    is_today_expiry = calendar.is_trading_day(today) and today == expiry_d

    # 3) Decide strike style
    if setup_type == "BREAKOUT":
        if is_today_expiry and use_otm_on_expiry_breakout:
            strike_style: StrikeStyle = "OTM_1"
        else:
            strike_style = "ATM"
    else:
        # REVERSAL (range bounce)
        strike_style = "OTM_1"

    # 4) Compute strike
    atm = _round_to_step(spot_price, strike_step)

    if strike_style == "ATM":
        strike = atm
    else:
        if direction == "LONG_CALL":
            strike = atm + strike_step
        else:
            strike = atm - strike_step

    opt_type = "CE" if direction == "LONG_CALL" else "PE"

    symbol = fyers_nifty_option_symbol(
        underlying=underlying,
        expiry_d=expiry_d,
        strike=strike,
        opt_type=opt_type,
        calendar=calendar,
        expiry_weekday=expiry_weekday,
        exchange_prefix=exchange_prefix,
    )

    notes = (
        f"spot≈{spot_price:.1f}, ATM={atm}, style={strike_style}, "
        f"expiry={expiry_d.isoformat()}, symbol={symbol}"
    )

    return OptionSelection(
        direction=direction,
        setup_type=setup_type,
        strike_style=strike_style,
        strike=strike,
        expiry_date=expiry_d,
        symbol=symbol,
        notes=notes,
    )
