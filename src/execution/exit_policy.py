"""Pure, fail-closed exit-policy evaluation.

This module only emits a decision. It never places, cancels, or replaces orders.
A caller must supply an executable bid from a fresh order book; midpoint marks
are deliberately not accepted as evidence that a SELL can execute.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class ExitDecision:
    should_exit: bool
    reason: str
    executable_bid: Decimal | None = None
    gross_pnl_usd: Decimal | None = None


def _d(value, field):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"invalid {field}") from None
    if not result.is_finite():
        raise ValueError(f"invalid {field}")
    return result


def evaluate_exit(*, enabled: bool, entry_price, shares, opened_at,
                  best_bid, book_timestamp, now=None, take_profit_pct=None,
                  stop_loss_pct=None, max_hold_seconds=None,
                  max_book_age_seconds=3):
    """Evaluate TP/SL/time stop against a fresh executable bid.

    Thresholds are fractional returns on entry price (e.g. 0.20 = +20%).
    Fees are intentionally excluded from trigger math until verified fee
    amounts are available. The returned gross P&L is not net realized P&L.
    """
    if not enabled:
        return ExitDecision(False, "disabled")
    entry = _d(entry_price, "entry_price")
    qty = _d(shares, "shares")
    if not (Decimal("0") < entry < Decimal("1")) or qty <= 0:
        return ExitDecision(False, "invalid_position")
    if not isinstance(opened_at, datetime) or opened_at.tzinfo is None:
        return ExitDecision(False, "invalid_open_time")
    if not isinstance(book_timestamp, datetime) or book_timestamp.tzinfo is None:
        return ExitDecision(False, "missing_fresh_book_timestamp")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        return ExitDecision(False, "invalid_clock")
    age = (now.astimezone(timezone.utc) - book_timestamp.astimezone(timezone.utc)).total_seconds()
    if age < -1 or age > max(0.0, float(max_book_age_seconds)):
        return ExitDecision(False, "stale_or_clock_skewed_book")
    bid = _d(best_bid, "best_bid")
    if not (Decimal("0") < bid < Decimal("1")):
        return ExitDecision(False, "invalid_executable_bid")
    held = (now.astimezone(timezone.utc) - opened_at.astimezone(timezone.utc)).total_seconds()
    if held < 0:
        return ExitDecision(False, "opened_in_future")
    ret = (bid - entry) / entry
    gross = (bid - entry) * qty

    # Hard risk stops take precedence over profit-taking.
    if stop_loss_pct is not None and ret <= -_d(stop_loss_pct, "stop_loss_pct"):
        return ExitDecision(True, "stop_loss", bid, gross)
    if max_hold_seconds is not None and held >= float(max_hold_seconds):
        return ExitDecision(True, "max_holding_time", bid, gross)
    if take_profit_pct is not None and ret >= _d(take_profit_pct, "take_profit_pct"):
        return ExitDecision(True, "take_profit", bid, gross)
    return ExitDecision(False, "hold", bid, gross)
