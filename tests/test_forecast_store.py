from datetime import datetime, timedelta, timezone

import pytest

from src.research.forecast_store import ForecastStore, ForecastStoreError
from src.research.scorecard import build_strategy_scorecard


def _times():
    observed = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return observed, observed + timedelta(days=7)


def test_forecast_store_keeps_unmodeled_observations_out_of_probability_score(tmp_path):
    store = ForecastStore(tmp_path / "forecasts.db")
    observed, resolution = _times()
    store.record_observation(
        forecast_id="f1", strategy="weekly", market_id="m1",
        observed_at=observed, resolution_at=resolution, probability_yes=None,
        entry_price=0.8, fees_per_share=0.01, slippage_per_share=0.002,
        metadata={"source": "gamma_clob"},
    )
    store.record_resolution(
        forecast_id="f1", event_id="r1", outcome_yes=True,
        resolved_at=resolution + timedelta(hours=1), resolution_source="official_market_resolution",
    )
    assert store.counts() == {"observations": 1, "resolved": 1, "modeled_observations": 0}
    assert store.load_resolved() == []


def test_forecast_store_loads_only_post_resolution_labeled_forecasts(tmp_path):
    store = ForecastStore(tmp_path / "forecasts.db")
    observed, resolution = _times()
    store.record_observation(
        forecast_id="f2", strategy="weekly", market_id="m2",
        observed_at=observed, resolution_at=resolution, probability_yes=0.9,
        entry_price=0.8, fees_per_share=0.01, slippage_per_share=0.01,
    )
    with pytest.raises(ForecastStoreError):
        store.record_resolution(
            forecast_id="f2", event_id="bad", outcome_yes=True,
            resolved_at=observed, resolution_source="test",
        )
    store.record_resolution(
        forecast_id="f2", event_id="r2", outcome_yes=True,
        resolved_at=resolution + timedelta(hours=1), resolution_source="official_resolution",
    )
    rows = store.load_resolved("weekly")
    assert len(rows) == 1 and rows[0].outcome_yes is True


def test_forecast_store_is_idempotent_but_rejects_conflicting_ids(tmp_path):
    store = ForecastStore(tmp_path / "forecasts.db")
    observed, resolution = _times()
    args = dict(
        forecast_id="f3", strategy="weekly", market_id="m3",
        observed_at=observed, resolution_at=resolution, probability_yes=0.5,
        entry_price=0.5, fees_per_share=0.01, slippage_per_share=0.01,
    )
    store.record_observation(**args)
    store.record_observation(**args)
    with pytest.raises(ForecastStoreError):
        store.record_observation(**{**args, "entry_price": 0.6})


def test_scorecard_reports_net_costs_calibration_and_drawdown():
    observed, resolution = _times()
    from src.research.walk_forward import ForecastOutcome
    rows = [
        ForecastOutcome("weekly", "m1", observed, resolution, 0.9, 0.8, True, 0.01, 0.01),
        ForecastOutcome("weekly", "m2", observed + timedelta(hours=1), resolution + timedelta(hours=1), 0.9, 0.8, False, 0.01, 0.01),
    ]
    score = build_strategy_scorecard(rows)
    assert score["sample_size"] == 2
    assert score["win_rate"] == 0.5
    assert score["mean_net_pnl_per_share"] == pytest.approx(-0.22)
    assert score["modeled_fees_per_share_total"] == pytest.approx(0.02)
    assert score["brier_score"] == pytest.approx(0.41)
