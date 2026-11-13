"""
Self-learning layer: calibration tracking + adaptive thresholds + regime detection.
"""
from collections import deque
from typing import Dict, Deque, Any
import logging
import math
import time
import numpy as np

logger = logging.getLogger("astf.learning")


def _clamp(x: float, lo: float, hi: float) -> float:
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return lo
        return max(lo, min(hi, v))
    except (TypeError, ValueError):
        return lo


class AdaptiveLearner:
    def __init__(self, config: dict):
        self.window = int(config.get("learning", {}).get("calibration_window", 200) or 200)
        self.regime_lookback = int(config.get("learning", {}).get("regime_lookback", 50) or 50)
        self.default_min = _clamp(config.get("signals", {}).get("min_score", 0.55), 0.40, 0.85)
        self.trade_log: Deque[Dict] = deque(maxlen=max(20, self.window))
        self.current_min_score = float(self.default_min)
        self.regime = "normal"
        self._last_adapt_ts = 0.0
        self._idle_cycles = 0

    def log_trade(self, trade: Dict):
        if not isinstance(trade, dict):
            return
        self.trade_log.append(trade)
        self._idle_cycles = 0
        if trade.get("stale"):
            return
        if len(self.trade_log) >= 15:
            self._adapt()

    def on_cycle(self, opened: int = 0):
        """
        Call every main loop cycle. If we sit idle under a sky-high min_score
        (classic chaotic freeze), gently decay back toward default.
        """
        if opened > 0:
            self._idle_cycles = 0
            return

        self._idle_cycles += 1
        # ~30 cycles of no opens at 10s ≈ 5 minutes
        if self._idle_cycles < 30:
            return
        if self.current_min_score <= self.default_min + 0.02:
            return

        # Decay toward default every 30 idle cycles
        old = self.current_min_score
        self.current_min_score = 0.7 * self.current_min_score + 0.3 * self.default_min
        self.current_min_score = _clamp(self.current_min_score, 0.45, 0.72)
        if self.regime == "chaotic" and self.current_min_score < 0.65:
            self.regime = "normal"
        self._idle_cycles = 0
        logger.info(
            f"Idle decay | min_score {old:.3f} → {self.current_min_score:.3f} | regime={self.regime}"
        )

    def _adapt(self):
        recent = [t for t in list(self.trade_log)[-self.regime_lookback:] if not t.get("stale")]
        if len(recent) < 8:
            return

        pnls = []
        for t in recent:
            try:
                p = float(t.get("pnl", 0) or 0)
                if math.isfinite(p):
                    pnls.append(p)
            except (TypeError, ValueError):
                continue
        if len(pnls) < 8:
            return

        win_rate = sum(1 for p in pnls if p > 0) / len(pnls)
        avg_edge = float(np.mean(pnls))
        volatility_of_pnl = float(np.std(pnls)) if len(pnls) > 5 else 0.0

        # Cap chaotic threshold lower than before so we can still trade
        if volatility_of_pnl > 0.05 and win_rate < 0.45:
            self.regime = "chaotic"
            self.current_min_score = min(0.70, self.current_min_score + 0.015)
        elif avg_edge > 0.003 and win_rate > 0.52:
            self.regime = "favorable"
            self.current_min_score = max(0.48, self.current_min_score - 0.02)
        else:
            self.regime = "normal"
            self.current_min_score = 0.85 * self.current_min_score + 0.15 * self.default_min

        self.current_min_score = _clamp(self.current_min_score, 0.45, 0.72)
        self._last_adapt_ts = time.time()

        logger.info(
            f"Adapted | regime={self.regime} | min_score={self.current_min_score:.3f} | "
            f"win_rate={win_rate:.2%} | avg_pnl={avg_edge:.4f} | n={len(pnls)}"
        )

    def get_min_score(self) -> float:
        return float(self.current_min_score)

    def get_regime(self) -> str:
        return self.regime if self.regime in ("normal", "favorable", "chaotic") else "normal"

    def export_state(self) -> Dict[str, Any]:
        return {
            "min_score": self.current_min_score,
            "regime": self.regime,
            "trade_count": len(self.trade_log),
        }

    def import_state(self, data: Dict[str, Any]):
        if not data or not isinstance(data, dict):
            return
        if "min_score" in data:
            # Soft-cap imported chaotic freeze from older runs
            self.current_min_score = _clamp(data["min_score"], 0.45, 0.72)
        if "regime" in data:
            r = str(data["regime"])
            if r in ("normal", "favorable", "chaotic"):
                self.regime = r
