from datetime import datetime, timedelta, timezone

import pytest

from src.execution.paper_exit_lifecycle import PaperExitLifecycle, PaperLifecycleError
from src.execution.exit_policy import evaluate_exit


def test_entry_partial_exit_fee_pnl_and_restart_replay(tmp_path):
    path = tmp_path / "paper-lifecycle.db"
    opened = datetime.now(timezone.utc) - timedelta(seconds=90)
    ledger = PaperExitLifecycle(path)
    ledger.record_entry_fill(position_id="p1", token_id="tok", market_id="m1",
        direction="up", quantity=4, price=.40, fee_usd=.01,
        opened_at=opened.isoformat(), event_id="buy-1")
    ledger.record_entry_fill(position_id="p1", token_id="tok", market_id="m1",
        direction="up", quantity=6, price=.50, fee_usd=.03,
        opened_at=opened.isoformat(), event_id="buy-2")
    pos = ledger.get_position("p1")
    net_shares = 10 - (.01 / .40) - (.03 / .50)
    assert pos["remaining_quantity"] == pytest.approx(net_shares)
    assert pos["average_entry_price"] == pytest.approx(4.60 / net_shares)

    now = datetime.now(timezone.utc)
    decision = evaluate_exit(enabled=True, entry_price=pos["average_entry_price"],
        shares=pos["remaining_quantity"], opened_at=opened,
        best_bid=.62, book_timestamp=now, take_profit_pct=.20,
        stop_loss_pct=.15, max_hold_seconds=180)
    assert decision.should_exit and decision.reason == "take_profit"

    ledger.record_exit_fill(position_id="p1", event_id="sell-1",
        quantity=4, price=.61, fee_usd=.02)
    # Replayed acknowledgement/event is idempotent.
    ledger.record_exit_fill(position_id="p1", event_id="sell-1",
        quantity=4, price=.61, fee_usd=.02)
    restored = PaperExitLifecycle(path)
    pos2 = restored.get_position("p1")
    assert pos2["remaining_quantity"] == pytest.approx(net_shares - 4)
    assert pos2["exit_quantity"] == 4
    expected_avg = 4.60 / net_shares
    expected_gross = (.61 - expected_avg) * 4
    assert pos2["realized_gross_pnl_usd"] == pytest.approx(expected_gross)
    assert pos2["allocated_entry_fees_usd"] == pytest.approx(.04 * 4 / net_shares)
    assert pos2["exit_fees_usd"] == pytest.approx(.02)
    assert pos2["realized_net_pnl_usd"] == pytest.approx(expected_gross - .02)


def test_exit_cannot_oversell_and_dedupe_mismatch_rejected(tmp_path):
    ledger = PaperExitLifecycle(tmp_path / "paper.db")
    ledger.record_entry_fill(position_id="p", token_id="t", market_id="m",
        direction="up", quantity=2, price=.5, fee_usd=.01, event_id="b1")
    with pytest.raises(PaperLifecycleError):
        ledger.record_exit_fill(position_id="p", event_id="s1", quantity=2.1, price=.6, fee_usd=.01)
    ledger.record_exit_fill(position_id="p", event_id="s1", quantity=1, price=.6, fee_usd=.01)
    with pytest.raises(PaperLifecycleError):
        ledger.record_exit_fill(position_id="p", event_id="s1", quantity=.5, price=.6, fee_usd=.01)


def test_summary_reports_realized_net_paper_pnl_and_fees(tmp_path):
    ledger = PaperExitLifecycle(tmp_path / "paper.db")
    ledger.record_entry_fill(position_id="p", token_id="t", market_id="m",
        direction="up", quantity=10, price=.4, fee_usd=.04, event_id="b1")
    ledger.record_exit_fill(position_id="p", event_id="s1", quantity=9.9, price=.5, fee_usd=.05)
    summary = ledger.summary()
    assert summary["gross_realized_pnl_usd"] == pytest.approx(.95)
    assert summary["fees_usd"] == pytest.approx(.09)
    assert summary["net_realized_pnl_usd"] == pytest.approx(.90)


def test_authoritative_resolution_closes_ledger_after_partial_exit(tmp_path):
    ledger = PaperExitLifecycle(tmp_path / "paper.db")
    ledger.record_entry_fill(position_id="p", token_id="t", market_id="m",
        direction="up", quantity=10, price=.40, fee_usd=.04, event_id="b1")
    ledger.record_exit_fill(position_id="p", event_id="s1",
        quantity=4, price=.50, fee_usd=.02)
    settled = ledger.record_settlement(position_id="p", event_id="settle:p",
        resolution_price=1.0)
    assert settled["remaining_quantity"] == 0
    assert settled["is_open"] is False
    assert settled["exit_quantity"] == pytest.approx(9.9)
    assert settled["realized_gross_pnl_usd"] == pytest.approx(3.90)
    assert settled["realized_net_pnl_usd"] == pytest.approx(3.88)
    assert ledger.open_positions() == []
    assert ledger.record_settlement(position_id="p", event_id="settle:p",
        resolution_price=1.0)["is_open"] is False


def test_paper_lifecycle_sync_restores_partial_position_after_restart(tmp_path):
    from src.execution.positions import PositionTracker
    from src.main import _sync_paper_ledger

    ledger = PaperExitLifecycle(tmp_path / "paper.db")
    ledger.record_entry_fill(position_id="p", token_id="t", market_id="m",
        direction="up", quantity=10, price=.40, fee_usd=.04,
        opened_at=datetime.now(timezone.utc).isoformat(),
        metadata={"asset": "btc", "window": "5m"}, event_id="b1")
    ledger.record_exit_fill(position_id="p", event_id="s1",
        quantity=4, price=.50, fee_usd=.02)

    restarted = PositionTracker()
    _sync_paper_ledger(restarted, ledger)
    restored = restarted.open["p"]
    assert restored.shares == pytest.approx(5.9)
    assert restored.entry_price == pytest.approx(4.0 / 9.9)
    assert restored.fee == pytest.approx(0.0)


def test_position_tracker_partial_close_reduces_remaining_shares_and_entry_fee():
    from src.execution.positions import PositionTracker
    tracker = PositionTracker()
    tracker.add({
        "order_id": "p", "market_id": "m", "token_id": "t", "side": "buy",
        "fill_price": .40, "effective_entry_price": 4.0 / 9.9,
        "size_usd": 4.0, "shares": 10.0, "net_shares": 9.9,
        "residual_fee_usd": 0.0, "fee": .04,
    }, direction="up")
    trade = tracker.partial_close("p", 4, .50, .02, "take_profit", "sell:p:4")
    assert trade["gross_pnl"] == pytest.approx((.50 - 4.0 / 9.9) * 4)
    assert trade["pnl"] == pytest.approx(trade["gross_pnl"] - .02)
    assert tracker.open["p"].shares == pytest.approx(5.9)
    assert tracker.open["p"].fee == pytest.approx(0.0)
