from src.signals.scorer import SignalScorer

def test_scorer_basic():
    config = {"signals": {"min_score": 0.5}}
    scorer = SignalScorer(config)
    features = {
        "momentum": 0.4,
        "book_imbalance": 0.3,
        "volatility_regime": "normal",
        "time_remaining_pct": 0.4,
        "mid_price": 0.55,
    }
    result = scorer.score(features)
    assert "score" in result
    assert 0 <= result["score"] <= 1
    assert result["direction"] in ("up", "down")
