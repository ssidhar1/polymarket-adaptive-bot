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


def test_uncalibrated_probability_proxy_never_becomes_tradeable_by_default():
    ens = EnsembleSignal(_cfg())
    feat = {
        "momentum": 0.8,
        "vs_open": 0.8,
        "book_imbalance": 0.8,
        "microprice_delta": 0.8,
        "time_remaining_pct": 0.3,
        "time_remaining_s": 90,
        "volatility_regime": "normal",
        "mid_price": 0.30,
        "ask_price": 0.30,
        "spread": 0.005,
        "depth_usd": 1000.0,
        "window_seconds": 300,
        "side": "up",
    }
    sig = ens.directional_score(feat, preferred_side="up")
    assert sig["pass"] is False
    assert sig["reason"] == "uncalibrated_probability"
    assert sig["est_prob"] is None
    assert sig["edge_after_fees"] is None
    assert sig["probability_kind"] == "uncalibrated_heuristic_proxy"


def test_explicit_heuristic_paper_probe_is_labeled_and_capped_by_risk_path():
    cfg = _cfg()
    cfg["signals"]["allow_heuristic_paper_entries"] = True
    ens = EnsembleSignal(cfg)
    feat = {
        "momentum": 0.8,
        "vs_open": 0.8,
        "book_imbalance": 0.8,
        "microprice_delta": 0.8,
        "time_remaining_pct": 0.3,
        "time_remaining_s": 90,
        "volatility_regime": "normal",
        "mid_price": 0.30,
        "ask_price": 0.30,
        "spread": 0.005,
        "depth_usd": 1000.0,
        "window_seconds": 300,
        "side": "up",
    }
    sig = ens.directional_score(feat, preferred_side="up")
    assert sig["pass"] is True
    assert sig["paper_probe"] is True
    assert sig["probability_kind"] == "uncalibrated_heuristic_proxy"


def test_market_specific_zero_fee_is_not_replaced_by_config_default():
    ens = EnsembleSignal(_cfg())
    assert ens._effective_fee(0.50, 0.0) == 0.001  # configured 20 bps slippage only
    assert ens._effective_fee(0.50, 0.07, 2.0) == 0.005375


def test_complete_set_cost_uses_explicit_zero_market_fee():
    ens = EnsembleSignal(_cfg())
    arb = ens.complete_set_arb(
        0.5, 0.5, up_ask=0.495, down_ask=0.495, fee_rate=0.0,
    )
    assert arb is not None
    assert arb["edge"] > 0
