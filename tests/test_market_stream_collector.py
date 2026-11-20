import json

from src.data.market_stream_collector import _decode_payload


def test_decode_legacy_market_channel_event():
    payload = {
        "event_type": "book",
        "asset_id": "token",
        "timestamp": "1782753357257",
        "bids": [{"price": "0.49", "size": "3"}],
        "asks": [{"price": "0.51", "size": "4"}],
    }
    assert _decode_payload(json.dumps(payload)) == [payload]


def test_decode_batched_market_events():
    events = [
        {"event_type": "last_trade_price", "asset_id": "token", "timestamp": "1000"},
        {"event_type": "tick_size_change", "asset_id": "token", "timestamp": "1001"},
    ]
    assert _decode_payload(json.dumps(events)) == events


def test_decode_heartbeat_frames_as_no_market_events():
    assert _decode_payload("PONG") == []
    assert _decode_payload("PING") == []
