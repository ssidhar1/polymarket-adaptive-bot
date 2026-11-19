from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.execution.exit_policy import evaluate_exit
from src.execution.fee_accounting import explicit_fee_usdc, fee_coverage


def test_exit_policy_disabled_by_default():
    now = datetime.now(timezone.utc)
    d = evaluate_exit(enabled=False, entry_price=".50", shares="10",
                      opened_at=now, best_bid=".70", book_timestamp=now)
    assert not d.should_exit
    assert d.reason == "disabled"


def test_take_profit_uses_executable_bid():
    now = datetime.now(timezone.utc)
    d = evaluate_exit(enabled=True, entry_price=".50", shares="10",
                      opened_at=now-timedelta(seconds=10), best_bid=".61",
                      book_timestamp=now, take_profit_pct=".20")
    assert d.should_exit and d.reason == "take_profit"
    assert d.gross_pnl_usd == Decimal("1.10")


def test_stop_loss_has_priority_over_time_stop():
    now = datetime.now(timezone.utc)
    d = evaluate_exit(enabled=True, entry_price=".50", shares="10",
                      opened_at=now-timedelta(seconds=100), best_bid=".40",
                      book_timestamp=now, stop_loss_pct=".10",
                      max_hold_seconds=30, take_profit_pct=".20")
    assert d.should_exit and d.reason == "stop_loss"


def test_time_stop_uses_utc_aware_timestamps():
    now = datetime.now(timezone.utc)
    d = evaluate_exit(enabled=True, entry_price=".50", shares="10",
                      opened_at=now-timedelta(seconds=61), best_bid=".51",
                      book_timestamp=now, max_hold_seconds=60)
    assert d.should_exit and d.reason == "max_holding_time"


def test_stale_book_fails_closed():
    now = datetime.now(timezone.utc)
    d = evaluate_exit(enabled=True, entry_price=".50", shares="10",
                      opened_at=now, best_bid=".90",
                      book_timestamp=now-timedelta(seconds=5),
                      take_profit_pct=".10", max_book_age_seconds=3)
    assert not d.should_exit and d.reason == "stale_or_clock_skewed_book"


def test_midpoint_is_not_an_input_or_exit_signal():
    now = datetime.now(timezone.utc)
    d = evaluate_exit(enabled=True, entry_price=".50", shares="10",
                      opened_at=now, best_bid=None, book_timestamp=now,
                      take_profit_pct=".10")
    assert not d.should_exit and d.reason == "invalid_executable_bid"


def test_fee_rate_is_not_misrepresented_as_fee_amount():
    assert explicit_fee_usdc({"fee_rate_bps": "700"}) is None
    assert explicit_fee_usdc({"fee_usdc": "0.0123"}) == Decimal("0.0123")


def test_invalid_explicit_fee_is_rejected():
    with pytest.raises(ValueError):
        explicit_fee_usdc({"fee_usdc": "-0.01"})


def test_fee_coverage_does_not_claim_net_pnl_from_partial_data():
    result = fee_coverage([
        {"fee_amount_usdc": "0.01"},
        {"fee_amount_usdc": None},
    ])
    assert result["missing_fee_count"] == 1
    assert result["explicit_fees_usdc"] is None
    assert result["net_realized_pnl_available"] is False
