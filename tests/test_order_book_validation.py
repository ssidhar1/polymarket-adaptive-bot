import math

from src.data.features import build_features, compute_book_imbalance, compute_depth_usd
from src.data.polymarket_client import _parse_book_levels


def test_clob_book_parser_drops_nonfinite_and_invalid_levels():
    levels = [
        {"price": 0.49, "size": 10},
        {"price": float("nan"), "size": 100},
        {"price": 0.50, "size": float("inf")},
        {"price": 1.01, "size": 5},
        {"price": 0.48, "size": -1},
        {"price": 0.47, "size": 4},
    ]
    assert _parse_book_levels(levels) == [
        {"price": 0.49, "size": 10.0},
        {"price": 0.47, "size": 4.0},
    ]


def test_features_sort_and_ignore_invalid_book_levels():
    market = {"_window_seconds": 300, "slug": "btc-updown-5m"}
    book = {
        "bids": [
            {"price": 0.48, "size": 10},
            {"price": float("nan"), "size": 100},
            {"price": 0.49, "size": 5},
        ],
        "asks": [
            {"price": 0.52, "size": 10},
            {"price": float("inf"), "size": 10},
            {"price": 0.51, "size": 5},
        ],
        "mid": float("nan"),
    }
    features = build_features(market, book)
    assert features["book_valid"] is True
    assert features["book_invalid_level_count"] == 2
    assert features["bid_price"] == 0.49
    assert features["ask_price"] == 0.51
    assert features["mid_price"] == 0.50
    assert math.isfinite(features["depth_usd"])
    assert math.isfinite(features["book_imbalance"])
    assert math.isfinite(features["microprice"])


def test_crossed_or_one_sided_books_do_not_produce_executable_quotes():
    market = {"_window_seconds": 300, "slug": "btc-updown-5m"}
    crossed = {
        "bids": [{"price": 0.52, "size": 10}],
        "asks": [{"price": 0.51, "size": 10}],
        "mid": 0.515,
    }
    features = build_features(market, crossed)
    assert features["book_valid"] is False
    assert features["bid_price"] is None
    assert features["ask_price"] is None
    assert features["mid_price"] is None
    assert features["microprice"] is None
    assert features["volatility_regime"] == "unknown"

    one_sided = build_features(market, {"bids": [], "asks": [{"price": 0.51, "size": 10}]})
    assert one_sided["book_valid"] is False
    assert one_sided["ask_price"] is None


def test_aggregate_book_features_ignore_nonfinite_values():
    bids = [
        {"price": 0.49, "size": float("inf")},
        {"price": 0.48, "size": 5},
    ]
    asks = [
        {"price": 0.51, "size": 4},
        {"price": 0.50, "size": float("nan")},
    ]
    assert compute_book_imbalance(bids, asks) == -1.0 / 9.0
    assert compute_depth_usd(bids, asks) == 0.48 * 5 + 0.51 * 4
