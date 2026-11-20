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
        "rate": 0.05, "exponent": 1.0,
        "source": "clob_market_info", "verified": True,
    }
    assert second == first
    assert len(calls) == 1


def test_fee_rate_endpoint_zero_is_a_valid_fee_free_schedule():
    data = PolymarketData()
    calls = []

    def fake_get(url, params=None, timeout=8):
        calls.append(url)
        if url.endswith("/markets/condition"):
            return FakeResponse({})
        return FakeResponse({"base_fee": 0})

    data._get = fake_get
    info = data.get_fee_schedule("token", "condition")

    assert info["rate"] == 0
    assert info["verified"] is True
    assert info["source"] == "clob_fee_rate_bps"
    assert len(calls) == 2


def test_fee_rate_endpoint_converts_basis_points_to_coefficient():
    data = PolymarketData()
    data._get = lambda url, params=None, timeout=8: (
        FakeResponse({}) if url.endswith("/markets/condition")
        else FakeResponse({"base_fee": 700})
    )
    info = data.get_fee_schedule("token", "condition")
    assert info["rate"] == 0.07
    assert info["exponent"] == 1.0


def test_unknown_fee_schedule_uses_unverified_conservative_fallback():
    data = PolymarketData()
    data._get = lambda url, params=None, timeout=8: (_ for _ in ()).throw(RuntimeError("offline"))
    info = data.get_fee_schedule("token", "condition", default_rate=0.07)
    assert info["rate"] == 0.07
    assert info["verified"] is False
    assert info["source"] == "conservative_config_fallback"
