"""Regression: preferred_side + side-aligned features must not invert imbalance."""
from src.signals.ensemble import EnsembleSignal


def _cfg():
    return {
        "signals": {
            "min_score": 0.50,
            "fee_rate": 0.01,
            "min_depth_usd": 1.0,
            "min_time_remaining_pct": 0.05,
            "max_entry_price": 0.70,
            "min_abs_momentum": 0.05,
            "max_prob_lift": 0.10,
            "degen_mode": True,
        },
        "risk": {"min_edge_after_fees": 0.01},
    }


def test_down_side_aligned_imbalance_positive():
    ens = EnsembleSignal(_cfg())
    # Market leans DOWN; features built with side=down already positive imb
    feat = {
        "momentum": -0.4,  # BTC falling → supports down
        "book_imbalance": 0.35,  # already side-aligned for down
        "time_remaining_pct": 0.5,
        "time_remaining_s": 150,
        "volatility_regime": "normal",
        "mid_price": 0.45,
        "ask_price": 0.46,
        "spread": 0.02,
        "depth_usd": 100.0,
        "window_seconds": 300,
        "side": "down",
    }
    sig = ens.directional_score(feat, preferred_side="down")
    assert sig["direction"] == "down"
    # Should not reject as weak solely due to inverted imb
    assert sig.get("components", {}).get("imbalance", 0) > 0
    assert sig["score"] > 0.5 or sig.get("reason") not in ("weak_signal",)


def test_up_side_aligned():
    ens = EnsembleSignal(_cfg())
    feat = {
        "momentum": 0.4,
        "book_imbalance": 0.3,
        "time_remaining_pct": 0.5,
        "time_remaining_s": 150,
        "volatility_regime": "normal",
        "mid_price": 0.45,
        "ask_price": 0.46,
        "spread": 0.02,
        "depth_usd": 100.0,
        "window_seconds": 300,
        "side": "up",
    }
    sig = ens.directional_score(feat, preferred_side="up")
    assert sig["direction"] == "up"
    assert sig.get("components", {}).get("imbalance", 0) > 0
