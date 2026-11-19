from src.data.features import build_features


def test_microprice_moves_toward_heavier_opposite_queue():
    market = {"_window_seconds": 300, "slug": "btc-updown-5m"}
    book = {
        "bids": [{"price": 0.49, "size": 100}],
        "asks": [{"price": 0.51, "size": 10}],
        "mid": 0.50,
    }
    features = build_features(market, book, side="up")
    assert features["microprice"] > features["mid_price"]
    assert features["microprice_delta"] > 0
