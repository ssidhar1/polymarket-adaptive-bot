from src.data.market_event_journal import MarketEventJournal
from src.research.microstructure_replay import (
    generate_microstructure_decisions,
    run_microstructure_research,
)


def _journal(path):
    journal = MarketEventJournal(path)
    journal.append({
        "event_type": "book", "asset_id": "token", "timestamp": 1000,
        "bids": [{"price": "0.49", "size": "10"}],
        "asks": [{"price": "0.51", "size": "10"}],
    }, received_ts_ns=100)
    journal.append({
        "event_type": "price_change", "asset_id": "token", "timestamp": 1100,
        "price_changes": [
            {"asset_id": "token", "price": "0.49", "size": "30", "side": "BUY"},
            {"asset_id": "token", "price": "0.51", "size": "5", "side": "SELL"},
        ],
    }, received_ts_ns=200)
    return journal


def test_positive_ofi_and_microprice_confirmation_generates_paper_intent(tmp_path):
    journal = _journal(tmp_path / "events.sqlite")
    generated = generate_microstructure_decisions(
        journal, ["token"], market_ids={"token": "market"},
        resolution_prices={"token": 1.0}, fee_schedules={"token": {"rate": 0.0}},
        notional_usd=5, min_ofi_normalized=.15, min_microprice_edge_bps=1,
        max_spread_bps=500, min_total_depth_shares=1,
    )
    assert generated["observations"] == 2
    assert len(generated["decisions"]) == 1
    decision = generated["decisions"][0]
    assert decision["decision_received_ns"] == 200
    assert decision["market_id"] == "market"
    assert decision["resolution_price"] == 1.0
    assert decision["fee_rate"] == 0.0


def test_microstructure_research_replays_decisions_after_latency(tmp_path):
    journal = _journal(tmp_path / "events.sqlite")
    result = run_microstructure_research(
        journal, ["token"], market_ids={"token": "market"},
        resolution_prices={"token": 1.0}, fee_schedules={"token": {"rate": 0.0}},
        notional_usd=5, decision_latency_ms=0, max_book_age_ms=1,
        slippage_bps=0, max_slippage_bps=1000,
        min_ofi_normalized=.15, min_microprice_edge_bps=1,
        max_spread_bps=500, min_total_depth_shares=1,
    )
    assert result["candidate_signals"] == 1
    assert result["execution_replay"]["filled_count"] == 1
    assert result["execution_replay"]["resolved_count"] == 1
    assert result["execution_replay"]["realized_pnl_usd"] > 0


def test_disconnect_resets_previous_book_so_ofi_does_not_bridge_gap(tmp_path):
    journal = _journal(tmp_path / "events.sqlite")
    journal.record_connection_gap(["token"], received_ts_ns=250)
    journal.append({
        "event_type": "book", "asset_id": "token", "timestamp": 900,
        "bids": [{"price": "0.49", "size": "30"}],
        "asks": [{"price": "0.51", "size": "5"}],
    }, received_ts_ns=300)
    generated = generate_microstructure_decisions(
        journal, ["token"], min_ofi_normalized=.15,
        min_microprice_edge_bps=1, max_spread_bps=500,
    )
    # The post-gap snapshot is a new baseline; no cross-gap OFI is inferred.
    assert generated["decisions"] == []
