import asyncio
import json

from src.data.market_stream_collector import MarketStreamCollector, _decode_payload


class FakeJournal:
    def __init__(self):
        self.gaps = []

    def record_connection_gap(self, assets, *, reason):
        self.gaps.append((list(assets), reason))


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, value):
        self.sent.append(value)


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


def test_dynamic_subscription_updates_and_invalidates_removed_tokens():
    journal = FakeJournal()
    collector = MarketStreamCollector(["token-a", "token-b"], journal)
    websocket = FakeWebSocket()
    collector.websocket = websocket

    result = asyncio.run(collector.update_tokens(["token-b", "token-c"]))

    assert result == {"added": ["token-c"], "removed": ["token-a"]}
    assert collector.token_ids == ["token-b", "token-c"]
    assert journal.gaps and journal.gaps[0][0] == ["token-a"]
    assert json.loads(websocket.sent[0]) == {
        "assets_ids": ["token-c"], "operation": "subscribe"
    }
    assert json.loads(websocket.sent[1]) == {
        "assets_ids": ["token-a"], "operation": "unsubscribe"
    }
