import pytest

from src.utils.fees import edge_after_taker_fee, fee_on_notional, taker_fee_rate, taker_fee_usd


def test_polymarket_fee_formula_is_per_share_not_per_notional():
    # 100 shares at 0.50 with 7% coefficient => 1.75 USDC.
    assert taker_fee_usd(100, 0.50, 0.07) == pytest.approx(1.75)


def test_fee_on_notional_converts_to_shares_before_calculating():
    # $50 at 0.50 buys 100 shares; fee is $1.75, not $0.875.
    assert fee_on_notional(50, 0.50, 0.07) == pytest.approx(1.75)


def test_fee_is_symmetric_around_fifty_percent():
    assert taker_fee_usd(100, 0.30, 0.07) == pytest.approx(
        taker_fee_usd(100, 0.70, 0.07)
    )


def test_minimum_fee_rounding():
    assert taker_fee_usd(0.001, 0.50, 0.07) == pytest.approx(0.00001)
    assert taker_fee_usd(0, 0.50, 0.07) == 0


def test_edge_helper_subtracts_fee_per_share():
    expected = 0.60 - 0.50 - (0.07 * 0.50 * 0.50)
    assert edge_after_taker_fee(0.60, 0.50, 0.07) == pytest.approx(expected)
