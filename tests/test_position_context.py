from datetime import datetime, timezone

from src.execution.positions import PositionTracker


def test_vs_open_context_survives_position_export_restore_and_resolution():
    tracker = PositionTracker()
    order = {
        "order_id": "paper-1",
        "market_id": "btc-5m",
        "token_id": "token-up",
        "side": "buy",
        "fill_price": 0.50,
        "size_usd": 5.0,
        "shares": 10.0,
        "fee": 0.01,
    }
    tracker.add(order, direction="up", slug="btc-5m", meta={
        "asset": "btc",
        "window": "5m",
        "vs_open": 0.42,
        "hour_utc": 12,
    })

    restored = PositionTracker()
    restored.import_open(tracker.export_open())
    trade = restored.resolve("paper-1", 1.0)

    assert trade is not None
    assert trade["vs_open"] == 0.42
    assert trade["size_usd"] == 5.0
