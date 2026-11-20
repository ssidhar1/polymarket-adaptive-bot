import pytest

from src.signals.microstructure import compute_microstructure_features


def test_microstructure_features_compute_spread_microprice_and_imbalance():
    book = {
        "complete": True,
        "bids": [{"price": "0.49", "size": "20"}],
        "asks": [{"price": "0.51", "size": "10"}],
    }
    features = compute_microstructure_features(book)
    assert features["valid"] is True
    assert features["mid"] == pytest.approx(0.50)
    assert features["spread"] == pytest.approx(0.02)
    assert features["top_imbalance"] == pytest.approx(1 / 3)
    assert features["microprice"] > features["mid"]


def test_microstructure_ofi_detects_bid_addition_and_ask_withdrawal():
    previous = {
        "complete": True,
        "bids": [{"price": "0.49", "size": "10"}],
        "asks": [{"price": "0.51", "size": "10"}],
    }
    current = {
        "complete": True,
        "bids": [{"price": "0.49", "size": "15"}],
        "asks": [{"price": "0.51", "size": "5"}],
    }
    features = compute_microstructure_features(current, previous)
    assert features["ofi"] == pytest.approx(20)
    assert features["ofi_normalized"] == pytest.approx(1.0)
    assert features["depth_delta"] == pytest.approx(10)
    assert features["depth_delta_normalized"] == pytest.approx(0.5)
    assert features["ask_depth_disappearance_fraction"] == pytest.approx(0.5)
    assert features["depth_disappearance_imbalance"] < 0


def test_microstructure_refuses_incomplete_or_crossed_books():
    incomplete = compute_microstructure_features({"complete": False})
    crossed = compute_microstructure_features({
        "bids": [{"price": "0.52", "size": "10"}],
        "asks": [{"price": "0.51", "size": "10"}],
    })
    assert incomplete == {"valid": False, "reason": "incomplete_book"}
    assert crossed == {"valid": False, "reason": "crossed_or_locked_book"}


def test_microstructure_depth_imbalance_uses_multiple_levels():
    book = {
        "bids": [
            {"price": "0.49", "size": "10"},
            {"price": "0.48", "size": "10"},
        ],
        "asks": [
            {"price": "0.51", "size": "5"},
            {"price": "0.52", "size": "5"},
        ],
    }
    features = compute_microstructure_features(book, depth_levels=2)
    assert features["bid_depth"] == 20
    assert features["ask_depth"] == 10
    assert features["depth_imbalance"] == pytest.approx(1 / 3)


def test_ofi_counts_new_price_levels_entering_top_of_book():
    previous = {
        "bids": [{"price": "0.48", "size": "10"}],
        "asks": [{"price": "0.52", "size": "10"}],
    }
    current = {
        "bids": [{"price": "0.49", "size": "7"}],
        "asks": [{"price": "0.51", "size": "6"}],
    }
    features = compute_microstructure_features(current, previous)
    # Bid depth changes by -3; ask depth changes by -4; OFI is their difference.
    assert features["ofi"] == pytest.approx(1)
