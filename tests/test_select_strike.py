from datetime import datetime

from src.order_service import select_strike, round_to_nearest_strike


def test_rounding():
    assert round_to_nearest_strike(22437.5, 50) == 22450


def test_morning_atm_ce():
    strike = select_strike(
        spot_price=22437.5,
        opt_type="CE",
        signal_time=datetime(2025, 3, 10, 10, 0),
        is_expiry_day=False,
        iv_percent=13.0,
        momentum_flag=True,
    )
    assert strike == 22450  # ATM morning


def test_afternoon_itm_pe():
    strike = select_strike(
        spot_price=22437.5,
        opt_type="PE",
        signal_time=datetime(2025, 3, 10, 13, 30),
        is_expiry_day=False,
        iv_percent=13.0,
        momentum_flag=None,
    )
    assert strike == 22450 + 50  # 1-step ITM for PE


def test_late_expiry_deep_itm_ce():
    strike = select_strike(
        spot_price=22437.5,
        opt_type="CE",
        signal_time=datetime(2025, 3, 13, 15, 0),
        is_expiry_day=True,
        iv_percent=18.0,
        momentum_flag=False,
    )
    assert strike == 22450 - 100  # deep ITM CE late expiry
