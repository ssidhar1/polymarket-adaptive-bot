import pytest

from src.utils.fees import (edge_after_taker_fee, fee_on_notional, parse_fee_exponent,
                            parse_fee_schedule, taker_fee_rate, taker_fee_usd)


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


def test_five_decimal_rounding_allows_tiny_fees_to_round_to_zero():
    assert taker_fee_usd(0.001, 0.50, 0.07) == pytest.approx(0.00002)
    assert taker_fee_usd(0.0001, 0.50, 0.07) == 0
    assert taker_fee_usd(0, 0.50, 0.07) == 0


def test_edge_helper_subtracts_fee_per_share():
    expected = 0.60 - 0.50 - (0.07 * 0.50 * 0.50)
    assert edge_after_taker_fee(0.60, 0.50, 0.07) == pytest.approx(expected)


def test_explicit_fee_free_market_rate_is_not_replaced_by_default():
    assert parse_fee_schedule({"_fee_rate": 0.0}, default_rate=0.07) == 0.0
    assert parse_fee_schedule({"feesEnabled": False}, default_rate=0.07) == 0.0


def test_market_fee_exponent_is_parsed():
    assert parse_fee_exponent({"_fee_exponent": 2.0}) == 2.0
    assert parse_fee_exponent({"_fee_exponent": float("nan")}) == 1.0


def test_non_finite_fee_inputs_fail_closed():
    assert taker_fee_rate(float("nan"), rate=0.07) == 0.0
    assert taker_fee_usd(float("inf"), 0.5, rate=0.07) == 0.0
    assert fee_on_notional(float("nan"), 0.5, rate=0.07) == 0.0


def test_fee_helper_preserves_fractional_market_exponents():
    price = 0.40
    expected = 0.07 * (price ** 2.5) * ((1.0 - price) ** 2.5)
    assert taker_fee_rate(price, rate=0.07, exponent=2.5) == pytest.approx(expected)


def test_fee_helper_rejects_invalid_exponents_and_caps_coefficients():
    assert taker_fee_rate(0.5, rate=0.07, exponent=float("nan")) == 0.0
    assert taker_fee_rate(0.5, rate=0.50, exponent=1.0) == pytest.approx(0.25 * 0.5 * 0.5)
