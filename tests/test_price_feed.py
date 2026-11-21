import src.data.price_feed as price_feed


def test_price_feed_backoff_prevents_repeated_requests(monkeypatch):
    clock = {"now": 100.0}
    monkeypatch.setattr(price_feed.time, "time", lambda: clock["now"])
    feed = price_feed.PriceFeed()
    calls = []
    values = {"BTCUSDT": 50000.0, "ETHUSDT": 3000.0}

    def fetch(symbol):
        calls.append(symbol)
        return values[symbol]

    feed._fetch_binance_price = fetch
    assert feed.get_prices() == {"btc": 50000.0, "eth": 3000.0}
    assert len(calls) == 2

    clock["now"] = 116.0
    values["ETHUSDT"] = None
    assert feed.get_prices() == {"btc": 50000.0}
    assert feed._fail_count == 1
    assert feed.health()["retry_in_seconds"] == 2.0
    calls_after_failure = len(calls)

    feed.last_fetch = 0.0
    clock["now"] = 117.0
    assert feed.get_prices() == {"btc": 50000.0}
    assert len(calls) == calls_after_failure


def test_price_feed_never_returns_cache_past_freshness_limit(monkeypatch):
    clock = {"now": 100.0}
    monkeypatch.setattr(price_feed.time, "time", lambda: clock["now"])
    feed = price_feed.PriceFeed()
    feed.cache = {"btc": 50000.0}
    feed.cache_updated_at = {"btc": 80.0}
    feed.last_fetch = 100.0
    assert feed.get_fresh_price("btc", max_age=15) is None
    assert feed.get_prices() == {}


def test_partial_price_feed_success_keeps_per_asset_freshness(monkeypatch):
    clock = {"now": 100.0}
    monkeypatch.setattr(price_feed.time, "time", lambda: clock["now"])
    feed = price_feed.PriceFeed()
    feed.cache = {"btc": 50000.0, "eth": 3000.0}
    feed.cache_updated_at = {"btc": 99.0, "eth": 80.0}
    feed.last_fetch = 0.0
    feed._fetch_binance_price = lambda symbol: 50100.0 if symbol == "BTCUSDT" else None
    assert feed.get_prices() == {"btc": 50100.0}
    assert feed.get_fresh_price("eth", max_age=15) is None



def test_price_feed_rejects_nonfinite_spot_values_and_cached_prices(monkeypatch):
    clock = {"now": 100.0}
    monkeypatch.setattr(price_feed.time, "time", lambda: clock["now"])
    feed = price_feed.PriceFeed()
    values = {"BTCUSDT": float("nan"), "ETHUSDT": float("inf")}
    feed._fetch_binance_price = lambda symbol: values[symbol]
    assert feed.get_prices() == {}
    assert feed.get_fresh_price("btc") is None
    assert feed.get_fresh_price("eth") is None

    feed.cache = {"btc": float("nan"), "eth": float("inf")}
    feed.cache_updated_at = {"btc": 100.0, "eth": 100.0}
    feed.last_fetch = 100.0
    assert feed.get_prices() == {}


def test_price_feed_ignores_nonfinite_klines_and_returns():
    feed = price_feed.PriceFeed()
    feed._fetch_klines = lambda *args, **kwargs: [
        100.0, float("nan"), 101.0, float("inf"), 102.0, 103.0,
    ]
    closes = feed._fetch_klines("BTCUSDT")
    assert closes == [100.0, 101.0, 102.0, 103.0]
    assert feed._ret([100.0, float("nan"), 102.0], 1) == 0.0
    assert feed._ret([100.0, float("inf")], 1) == 0.0
