import pytest

from src.data.market_event_journal import MarketEventError, MarketEventJournal


def test_journal_replays_snapshot_and_price_level_updates(tmp_path):
    journal = MarketEventJournal(tmp_path / "events.sqlite")
    journal.append({
        "event_type": "book", "asset_id": "token", "market": "condition",
        "timestamp": "1000",
        "bids": [{"price": "0.49", "size": "10"}],
        "asks": [{"price": "0.51", "size": "8"}],
    }, received_ts_ns=10)
    journal.append({
        "event_type": "price_change", "market": "condition", "timestamp": "1100",
        "price_changes": [
            {"asset_id": "token", "price": "0.50", "size": "4", "side": "BUY"},
            {"asset_id": "token", "price": "0.51", "size": "0", "side": "SELL"},
            {"asset_id": "token", "price": "0.52", "size": "7", "side": "SELL"},
        ],
    }, received_ts_ns=20)

    book = journal.replay_book("token", as_of_ms=1100)
    assert book["complete"] is True
    assert book["bids"] == [{"price": 0.5, "size": 4.0}, {"price": 0.49, "size": 10.0}]
    assert book["asks"] == [{"price": 0.52, "size": 7.0}]


def test_replay_respects_receive_time_to_prevent_lookahead(tmp_path):
    journal = MarketEventJournal(tmp_path / "events.sqlite")
    journal.append({
        "event_type": "book", "asset_id": "token", "timestamp": 1000,
        "bids": [], "asks": [{"price": "0.5", "size": "10"}],
    }, received_ts_ns=100)
    journal.append({
        "event_type": "price_change", "asset_id": "token", "timestamp": 1100,
        "price_changes": [{"asset_id": "token", "price": "0.4", "size": "5", "side": "BUY"}],
    }, received_ts_ns=300)

    before_late_event = journal.replay_book("token", as_of_ms=1200, received_before_ns=200)
    assert before_late_event["bids"] == []
    assert before_late_event["asks"][0]["price"] == 0.5


def test_replay_without_snapshot_is_not_marked_complete(tmp_path):
    journal = MarketEventJournal(tmp_path / "events.sqlite")
    journal.append({
        "event_type": "price_change", "asset_id": "token", "timestamp": 1000,
        "price_changes": [{"asset_id": "token", "price": "0.5", "size": "3", "side": "BUY"}],
    })
    book = journal.replay_book("token")
    assert book["has_snapshot"] is False
    assert book["complete"] is False
    assert book["bids"] == []


def test_journal_rejects_events_without_exchange_timestamp(tmp_path):
    journal = MarketEventJournal(tmp_path / "events.sqlite")
    with pytest.raises(MarketEventError):
        journal.append({"event_type": "book", "asset_id": "token"})


def test_batched_price_changes_are_replayed_for_each_token(tmp_path):
    journal = MarketEventJournal(tmp_path / "events.sqlite")
    journal.append({
        "event_type": "book", "asset_id": "token-a", "timestamp": 1000,
        "bids": [], "asks": [{"price": "0.5", "size": "10"}],
    })
    journal.append({
        "event_type": "book", "asset_id": "token-b", "timestamp": 1000,
        "bids": [], "asks": [{"price": "0.6", "size": "10"}],
    })
    journal.append({
        "event_type": "price_change", "timestamp": 1100,
        "price_changes": [
            {"asset_id": "token-a", "price": "0.49", "size": "3", "side": "BUY"},
            {"asset_id": "token-b", "price": "0.59", "size": "4", "side": "BUY"},
        ],
    })
    assert journal.replay_book("token-a")["bids"] == [{"price": 0.49, "size": 3.0}]
    assert journal.replay_book("token-b")["bids"] == [{"price": 0.59, "size": 4.0}]


def test_late_older_delta_invalidates_book_completeness(tmp_path):
    journal = MarketEventJournal(tmp_path / "events.sqlite")
    journal.append({
        "event_type": "book", "asset_id": "token", "timestamp": 2000,
        "bids": [], "asks": [{"price": "0.5", "size": "10"}],
    })
    journal.append({
        "event_type": "price_change", "asset_id": "token", "timestamp": 1900,
        "price_changes": [{"asset_id": "token", "price": "0.49", "size": "2", "side": "BUY"}],
    })
    book = journal.replay_book("token")
    assert book["out_of_order"] is True
    assert book["complete"] is False
