"""
Self-learning layer: calibration tracking + adaptive thresholds + simple regime detection.
"""
from collections import deque
from typing import Dict, Deque
import logging
import numpy as np

logger = logging.getLogger("astf.learning")

class AdaptiveLearner:
    def __init__(self, config: dict):
        self.window = config.get("learning", {}).get("calibration_window", 200)
        self.regime_lookback = config.get("learning", {}).get("regime_lookback", 50)
        self.trade_log: Deque[Dict] = deque(maxlen=self.window)
        self.current_min_score = config.get("signals", {}).get("min_score", 0.62)
        self.regime = "normal"

    def log_trade(self, trade: Dict):
        """
        trade should contain: score, direction, entry_price, exit_price (or resolution),
        pnl, features, timestamp
        """
        self.trade_log.append(trade)
        if len(self.trade_log) >= 30:
            self._adapt()

    def _adapt(self):
        recent = list(self.trade_log)[-self.regime_lookback:]
        pnls = [t.get("pnl", 0) for t in recent]
        scores = [t.get("score", 0.5) for t in recent]

        win_rate = sum(1 for p in pnls if p > 0) / len(pnls) if pnls else 0.5
        avg_edge = np.mean(pnls) if pnls else 0

        # Simple regime heuristic
        volatility_of_pnl = np.std(pnls) if len(pnls) > 5 else 0
        if volatility_of_pnl > 0.04:
            self.regime = "chaotic"
            self.current_min_score = min(0.78, self.current_min_score + 0.03)
        elif avg_edge > 0.01 and win_rate > 0.55:
            self.regime = "favorable"
            self.current_min_score = max(0.55, self.current_min_score - 0.02)
        else:
            self.regime = "normal"
            # slowly revert toward default
            self.current_min_score = 0.7 * self.current_min_score + 0.3 * 0.62

        logger.info(f"Adapted | regime={self.regime} | min_score={self.current_min_score:.3f} | win_rate={win_rate:.2%} | avg_edge={avg_edge:.4f}")

    def get_min_score(self) -> float:
        return self.current_min_score

    def get_regime(self) -> str:
        return self.regime
