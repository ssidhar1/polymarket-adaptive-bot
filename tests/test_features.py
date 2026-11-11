from src.data.features import (
    detect_window_seconds,
    compute_book_imbalance,
    build_features,
    _level_price,
    _level_size,
)

def test_detect_window():
    assert detect_window_seconds("btc-updown-5m-123") == 300
    assert detect_window_seconds("btc-updown-15m-123") == 900

def test_level_parsing():
    assert _level_price({"price": "0.42", "size": "10"}) == 0.42
    assert _level_size(["0.41", "5"]) == 5.0

def test_imbalance():
    bids = [{"price": 0.5, "size": 100}, {"price": 0.49, "size": 50}]
    asks = [{"price": 0.51, "size": 20}]
    imb = compute_book_imbalance(bids, asks)
    assert imb > 0

def test_build_features():
    market = {"slug": "btc-updown-15m-1", "endDate": None}
    book = {
        "bids": [{"price": 0.48, "size": 200}],
        "asks": [{"price": 0.52, "size": 150}],
        "mid": 0.5,
    }
    f = build_features(market, book, external_momentum=0.2)
    assert "book_imbalance" in f
    assert f["window_seconds"] == 900
    assert f["depth_usd"] > 0
