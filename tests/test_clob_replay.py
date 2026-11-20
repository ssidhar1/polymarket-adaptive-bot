import pytest

from src.data.market_event_journal import MarketEventJournal
from src.execution.clob_replay import CLOBReplayEngine


def _journal_with_book(path):
    journal = MarketEventJournal(path)
    journal.append({
        "event_type": "book", "asset_id": "token", "market": "condition",
        "timestamp": 1000,
        "bids": [{"price": "0.49", "size": "50"}],
        "asks": [{"price": "0.50", "size": "50"}],
    }, received_ts_ns=100)
    return journal


def test_replay_uses_submit_time_book_after_configured_latency(tmp_path):
    journal = _journal_with_book(tmp_path / "events.sqlite")
    journal.append({
        "event_type": "price_change", "market": "condition", "timestamp": 1100,
        "price_changes": [
            {"asset_id": "token", "price": "0.50", "size": "0", "side": "SELL"},
            {"asset_id": "token", "price": "0.52", "size": "100", "side": "SELL"},
        ],
    }, received_ts_ns=200)

    engine = CLOBReplayEngine(
        journal, decision_latency_ms=0.0001, max_book_age_ms=0.001,
        slippage_bps=0, max_slippage_bps=1000, default_fee_rate=0,
    )
    result = engine.replay([{
        "decision_id": "d1", "token_id": "token", "market_id": "condition",
        "decision_received_ns": 150, "notional_usd": 5.0, "resolution_price": 1.0,
        "fee_rate": 0.0,
    }])

    assert result["filled_count"] == 1
    fill = result["fills"][0]
    assert fill["order"]["requested_price"] == pytest.approx(0.52)
    assert fill["order"]["fill_price"] == pytest.approx(0.52)
    assert fill["settlement"]["pnl"] == pytest.approx((5 / 0.52) - 5)
    assert fill["decision_features"]["valid"] is True


def test_replay_does_not_leak_events_received_after_submit_cutoff(tmp_path):
    journal = _journal_with_book(tmp_path / "events.sqlite")
    journal.append({
        "event_type": "price_change", "market": "condition", "timestamp": 1100,
        "price_changes": [
            {"asset_id": "token", "price": "0.50", "size": "0", "side": "SELL"},
            {"asset_id": "token", "price": "0.60", "size": "100", "side": "SELL"},
        ],
    }, received_ts_ns=200)

    engine = CLOBReplayEngine(
        journal, decision_latency_ms=0.00002, max_book_age_ms=0.001,
        slippage_bps=0, max_slippage_bps=1000, default_fee_rate=0,
    )
    result = engine.replay([{
        "token_id": "token", "market_id": "condition",
        "decision_received_ns": 150, "notional_usd": 5.0,
    }])

    assert result["filled_count"] == 1
    assert result["fills"][0]["order"]["requested_price"] == pytest.approx(0.50)


def test_replay_rejects_connection_gap_between_decision_and_submit(tmp_path):
    journal = _journal_with_book(tmp_path / "events.sqlite")
    journal.record_connection_gap(["token"], reason="test disconnect", received_ts_ns=200)
    engine = CLOBReplayEngine(
        journal, decision_latency_ms=0.0001, max_book_age_ms=0.001,
        slippage_bps=0, default_fee_rate=0,
    )
    result = engine.replay([{
        "token_id": "token", "market_id": "condition",
        "decision_received_ns": 150, "notional_usd": 5.0,
    }])
    assert result["filled_count"] == 0
    assert result["rejections"][0]["reason"] == "submit_incomplete_book"


def test_replay_rejects_stale_decision_book(tmp_path):
    journal = _journal_with_book(tmp_path / "events.sqlite")
    engine = CLOBReplayEngine(
        journal, decision_latency_ms=0, max_book_age_ms=0.00001,
        slippage_bps=0, default_fee_rate=0,
    )
    result = engine.replay([{
        "token_id": "token", "market_id": "condition",
        "decision_received_ns": 150, "notional_usd": 5.0,
    }])
    assert result["filled_count"] == 0
    assert result["rejections"][0]["reason"] == "decision_stale_book"
