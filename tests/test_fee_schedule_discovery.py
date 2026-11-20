from src.data.polymarket_client import PolymarketData


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_market_info_fee_schedule_is_preferred_and_cached():
    data = PolymarketData()
    calls = []

    def fake_get(url, params=None, timeout=8):
        calls.append((url, params))
        return FakeResponse({"fd": {"r": 0.05, "e": 1}})

    data._get = fake_get
    first = data.get_fee_schedule("token", "condition")
    second = data.get_fee_schedule("token", "condition")

    assert first == {
        "rate": 0.05, "exponent": 1.0, "collection": "usdc",
        "source": "clob_market_info", "verified": True,
    }
    assert second == first
    assert len(calls) == 1


def test_legacy_fee_rate_endpoint_is_not_used_for_v2():
    data = PolymarketData()
    calls = []

    def fake_get(url, params=None, timeout=8):
        calls.append(url)
        return FakeResponse({})

    data._get = fake_get
    info = data.get_fee_schedule("token", "condition", default_rate=0.07)

    assert len(calls) == 1
    assert calls[0].endswith("/markets/condition")
    assert info["rate"] == 0.07
    assert info["verified"] is False
    assert info["source"] == "conservative_config_fallback"


def test_unknown_fee_schedule_uses_unverified_conservative_fallback():
    data = PolymarketData()
    data._get = lambda url, params=None, timeout=8: (_ for _ in ()).throw(RuntimeError("offline"))
    info = data.get_fee_schedule("token", "condition", default_rate=0.07)
    assert info["rate"] == 0.07
    assert info["verified"] is False
    assert info["source"] == "conservative_config_fallback"
