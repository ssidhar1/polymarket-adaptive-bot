"""
Multi-feature scoring for short-term crypto markets.
Returns a score between 0 and 1 indicating confidence in a directional edge.
"""
from typing import Dict, Optional
import numpy as np
import logging

logger = logging.getLogger("astf.signals")

class SignalScorer:
    def __init__(self, config: dict):
        self.min_score = config.get("signals", {}).get("min_score", 0.62)
        self.lookback = config.get("signals", {}).get("lookback_minutes", 30)

    def score(self, features: Dict) -> Dict:
        """
        features expected keys:
        - momentum: float (-1 to 1)
        - book_imbalance: float (-1 to 1)
        - volatility_regime: str ("low", "normal", "high")
        - time_remaining_pct: float (0-1)
        - mid_price: float (0-1)
        """
        momentum = features.get("momentum", 0.0)
        imbalance = features.get("book_imbalance", 0.0)
        time_left = features.get("time_remaining_pct", 0.5)
        vol = features.get("volatility_regime", "normal")

        # Simple weighted combination (to be improved by learning layer)
        base = 0.4 * momentum + 0.35 * imbalance + 0.15 * (1 - abs(time_left - 0.3))

        # Volatility adjustment
        if vol == "high":
            base *= 0.85
        elif vol == "low":
            base *= 1.05

        score = float(np.clip(base, 0.0, 1.0))

        direction = "up" if momentum + imbalance > 0 else "down"

        return {
            "score": score,
            "direction": direction,
            "pass": score >= self.min_score,
            "components": {
                "momentum": momentum,
                "imbalance": imbalance,
                "time_factor": time_left,
                "vol_regime": vol
            }
        }
