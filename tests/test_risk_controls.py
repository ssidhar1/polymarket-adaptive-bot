import pytest

from src.risk.manager import RiskManager


def _risk():
    return RiskManager({
        "risk": {
            "max_position_pct": 0.04,
            "max_concurrent": 6,
            "max_total_exposure_pct": 0.20,
            "max_asset_exposure_pct": 0.14,
            "max_window_exposure_pct": 0.18,
            "daily_loss_limit_pct": 0.05,
            "max_drawdown_pct": 0.12,
            "probability_model_calibrated": False,
        },
        "learning": {"exploration_pct": 0.02},
        "signals": {"degen_mode": True},
    }, 1000.0)


def test_portfolio_total_exposure_cap():
    risk = _risk()
    assert risk.can_open(30, reserved_exposure=160)
    assert not risk.can_open(50, reserved_exposure=160)


def test_asset_and_window_exposure_caps():
    risk = _risk()
    assert not risk.can_open(50, asset_exposure=100)
    assert not risk.can_open(30, window_exposure=160)


def test_uncalibrated_probability_cannot_trigger_kelly_sizing():
    risk = _risk()
    size = risk.size_position(
        confidence=0.60,
        est_prob=0.99,
        price=0.50,
        edge_after_fees=0.05,
    )
    assert size == pytest.approx(10.0)
    assert size <= 40.0


def test_daily_loss_pause_is_not_cleared_by_restart_api():
    risk = _risk()
    risk.record_pnl(-60.0)
    assert risk.paused
    assert not risk.can_open(10)
