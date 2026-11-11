from src.signals.ensemble import EnsembleSignal


def test_complete_set_arb():
    ens = EnsembleSignal({"signals": {"min_score": 0.6}, "risk": {}})
    arb = ens.complete_set_arb(0.46, 0.46)
    assert arb is not None
    assert arb["type"] == "complete_set"
    assert arb["edge"] > 0


def test_thin_book_reject():
    ens = EnsembleSignal({"signals": {"min_score": 0.5, "min_depth_usd": 100}})
    out = ens.directional_score({
        "momentum": 0.5,
        "book_imbalance": 0.4,
        "time_remaining_pct": 0.4,
        "volatility_regime": "normal",
        "mid_price": 0.55,
        "spread": 0.02,
        "depth_usd": 10,
    })
    assert out["pass"] is False
    assert out.get("reason") == "thin_book"


def test_near_expiry_reject():
    ens = EnsembleSignal({"signals": {"min_score": 0.5, "min_depth_usd": 1, "min_time_remaining_pct": 0.1}})
    out = ens.directional_score({
        "momentum": 0.5,
        "book_imbalance": 0.4,
        "time_remaining_pct": 0.02,
        "volatility_regime": "normal",
        "mid_price": 0.55,
        "spread": 0.02,
        "depth_usd": 500,
    })
    assert out["pass"] is False
    assert out.get("reason") == "near_expiry"
