from datetime import datetime, timedelta, timezone

import pytest

from src.strategies.weekly_probability import LognormalRangeModel
from src.research.walk_forward import ForecastOutcome, walk_forward_evaluate


def test_lognormal_probabilities_are_bounded_and_partition_mass():
    model = LognormalRangeModel()
    kwargs = dict(spot=100_000, annualized_volatility=0.70, hours_to_expiry=24 * 7)
    below = model.probability_below(threshold=100_000, **kwargs)
    above = model.probability_above(threshold=100_000, **kwargs)
    assert 0 <= below <= 1
    assert 0 <= above <= 1
    assert below + above == pytest.approx(1.0, abs=1e-9)


def test_lognormal_model_handles_bucket_boundaries_and_expiry():
    model = LognormalRangeModel()
    p = model.probability_between(
        spot=100, lower=95, upper=105, annualized_volatility=0.5, hours_to_expiry=24,
    )
    assert 0 < p < 1
    assert model.probability_between(
        spot=100, lower=95, upper=105, annualized_volatility=0.5, hours_to_expiry=0,
    ) == 1.0


def test_lognormal_model_rejects_invalid_market_inputs():
    with pytest.raises(ValueError):
        LognormalRangeModel().probability_between(
            spot=0, lower=90, upper=110, annualized_volatility=0.5, hours_to_expiry=24,
        )


def _record(i, outcome):
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=i)
    return ForecastOutcome(
        strategy="weekly", market_id=f"m{i}", observed_at=stamp,
        resolution_at=stamp + timedelta(days=1), probability_yes=0.8,
        entry_price=0.7, outcome_yes=outcome, fees_per_share=0.01,
        slippage_per_share=0.01,
    )


def test_walk_forward_uses_chronological_train_then_test_windows():
    records = [_record(i, i % 2 == 0) for i in range(7)]
    folds = walk_forward_evaluate(records, min_train=3, test_size=2)
    assert [f.train_count for f in folds] == [3, 5]
    assert [f.test_count for f in folds] == [2, 2]
    assert folds[0].test_start > records[2].observed_at
    assert folds[0].mean_net_pnl_per_share == pytest.approx(-0.22)


def test_walk_forward_rejects_lookahead_and_duplicate_market_rows():
    row = _record(0, True)
    invalid = ForecastOutcome(
        **{**row.__dict__, "observed_at": row.resolution_at + timedelta(seconds=1)}
    )
    with pytest.raises(ValueError):
        walk_forward_evaluate([invalid], min_train=1, test_size=1)
    with pytest.raises(ValueError):
        walk_forward_evaluate([row, row], min_train=1, test_size=1)
