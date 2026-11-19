import time

from src.data.price_feed import PriceFeed
from src import main


def test_sigterm_handler_only_sets_flag(monkeypatch):
    monkeypatch.setattr(main, "_shutdown", False)
    def forbidden_log(*args, **kwargs):
        raise AssertionError("signal handler must not render/log")
    monkeypatch.setattr(main.logger, "info", forbidden_log)
    main._handle_signal(15, None)
    assert main._shutdown is True


def test_price_feed_never_returns_cache_older_than_freshness_limit():
    feed = PriceFeed()
    feed.cache = {"btc": 100.0}
    feed.cache_updated_at = {"btc": time.time() - 16.0}
    feed.last_fetch = time.time()
    assert feed.get_prices() == {}
    assert feed.get_fresh_price("btc", max_age=15.0) is None
