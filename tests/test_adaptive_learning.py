from src.learning.adaptive import AdaptiveLearner


def _learner():
    return AdaptiveLearner({
        "signals": {"min_score": 0.55},
        "learning": {"min_regime_trades": 25, "min_bucket_trades": 25},
    })


def test_regime_does_not_adapt_on_small_sample():
    learner = _learner()
    for _ in range(10):
        learner.log_trade({"pnl": 1.0, "size_usd": 10.0, "hour_utc": 12})
    assert learner.get_regime() == "normal"
    assert learner.get_min_score() == 0.55


def test_bucket_bonus_requires_minimum_sample_and_positive_return():
    learner = _learner()
    key = (12, "early", "strong")
    assert learner.get_bucket_boost(12, 250, 0.5) == 0.0

    learner.buckets[key] = [20, 25, 0.25]
    assert learner.get_bucket_boost(12, 250, 0.5) > 0.0

    learner.buckets[key] = [20, 25, -0.25]
    assert learner.get_bucket_boost(12, 250, 0.5) == 0.0


def test_versioned_bucket_state_round_trips():
    learner = _learner()
    learner.buckets[(12, "early", "strong")] = [20, 25, 0.25]
    state = learner.export_state()

    restored = _learner()
    restored.import_state(state)
    assert restored.buckets[(12, "early", "strong")] == [20, 25, 0.25]


def test_legacy_usd_bucket_state_is_ignored():
    learner = _learner()
    learner.import_state({
        "min_score": 0.60,
        "regime": "favorable",
        "buckets": {"(12, 'early', 'strong')": [20, 25, 12.0]},
    })
    assert not learner.buckets
    assert learner.get_regime() == "favorable"
