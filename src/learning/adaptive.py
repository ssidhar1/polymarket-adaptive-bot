"""
Self-learning layer with setup-bucket tracking.
Buckets: hour_utc + remaining_bucket + vs_open_strength.
"""
from collections import deque, defaultdict
from typing import Dict, Deque, Any, Tuple
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
        self.window = int(config.get("learning", {}).get("calibration_window", 150) or 150)
        self.regime_lookback = int(config.get("learning", {}).get("regime_lookback", 40) or 40)
        self.default_min = _clamp(config.get("signals", {}).get("min_score", 0.54), 0.40, 0.80)
        self.trade_log: Deque[Dict] = deque(maxlen=max(30, self.window))
        self.current_min_score = float(self.default_min)
        self.regime = "normal"
        self._last_adapt_ts = 0.0
        self._idle_cycles = 0
        # Setup buckets: (hour, rem_bucket, open_bucket) -> [wins, total, pnl_sum]
        self.buckets: Dict[Tuple[int, str, str], list] = defaultdict(lambda: [0, 0, 0.0])

    def _bucket_key(self, trade: Dict) -> Tuple[int, str, str]:
        hour = int(trade.get("hour_utc") or 0)
        rem = float(trade.get("time_remaining_s") or 300)
        if rem < 90:
            rem_b = "late"
        elif rem < 180:
            rem_b = "mid"
        else:
            rem_b = "early"
        vo = abs(float(trade.get("vs_open") or trade.get("momentum") or 0))
        if vo > 0.35:
            open_b = "strong"
        elif vo > 0.15:
            open_b = "med"
        else:
            open_b = "weak"
        return (hour, rem_b, open_b)

    def log_trade(self, trade: Dict):
        if not isinstance(trade, dict):
            return
        self.trade_log.append(trade)
        self._idle_cycles = 0
        if trade.get("stale"):
            return
        key = self._bucket_key(trade)
        b = self.buckets[key]
        b[1] += 1
        pnl = float(trade.get("pnl") or 0)
        b[2] += pnl
        if pnl > 0:
            b[0] += 1
        if len(self.trade_log) >= 12:
            self._adapt()

    def get_bucket_boost(self, hour: int, remaining_s: float, vs_open: float) -> float:
        """Return small score/edge boost or penalty based on historical bucket."""
        if remaining_s < 90:
            rem_b = "late"
        elif remaining_s < 180:
            rem_b = "mid"
        else:
            rem_b = "early"
        vo = abs(vs_open)
        open_b = "strong" if vo > 0.35 else ("med" if vo > 0.15 else "weak")
        key = (hour, rem_b, open_b)
        b = self.buckets.get(key)
        if not b or b[1] < 6:
            return 0.0
        wr = b[0] / b[1]
        avg = b[2] / b[1]
        if wr > 0.58 and avg > 0.002:
            return 0.04
        if wr < 0.42 and avg < -0.001:
            return -0.05
        return 0.0

    def on_cycle(self, opened: int = 0):
        if opened > 0:
            self._idle_cycles = 0
            return
        self._idle_cycles += 1
        if self._idle_cycles < 25:
            return
        if self.current_min_score <= self.default_min + 0.015:
            return
        old = self.current_min_score
        self.current_min_score = 0.72 * self.current_min_score + 0.28 * self.default_min
        self.current_min_score = _clamp(self.current_min_score, 0.46, 0.70)
        if self.regime == "chaotic" and self.current_min_score < 0.62:
            self.regime = "normal"
        self._idle_cycles = 0
        logger.info(f"Idle decay | min_score {old:.3f} → {self.current_min_score:.3f}")

    def _adapt(self):
        recent = [t for t in list(self.trade_log)[-self.regime_lookback:] if not t.get("stale")]
        if len(recent) < 7:
            return
        pnls = [float(t.get("pnl") or 0) for t in recent if math.isfinite(float(t.get("pnl") or 0))]
        if len(pnls) < 7:
            return
        win_rate = sum(1 for p in pnls if p > 0) / len(pnls)
        avg_edge = float(np.mean(pnls))
        vol_pnl = float(np.std(pnls)) if len(pnls) > 4 else 0.0

        if vol_pnl > 0.06 and win_rate < 0.43:
            self.regime = "chaotic"
            self.current_min_score = min(0.68, self.current_min_score + 0.012)
        elif avg_edge > 0.004 and win_rate > 0.54:
            self.regime = "favorable"
            self.current_min_score = max(0.47, self.current_min_score - 0.025)
        else:
            self.regime = "normal"
            self.current_min_score = 0.82 * self.current_min_score + 0.18 * self.default_min

        self.current_min_score = _clamp(self.current_min_score, 0.46, 0.70)
        logger.info(
            f"Adapted | regime={self.regime} | min_score={self.current_min_score:.3f} | "
            f"wr={win_rate:.1%} | avg={avg_edge:.4f} | buckets={len(self.buckets)}"
        )

    def get_min_score(self) -> float:
        return float(self.current_min_score)

    def get_regime(self) -> str:
        return self.regime

    def export_state(self) -> Dict[str, Any]:
        # Persist top buckets only to keep state small
        top = sorted(
            [(k, v) for k, v in self.buckets.items() if v[1] >= 4],
            key=lambda x: -x[1][1],
        )[:40]
        return {
            "min_score": self.current_min_score,
            "regime": self.regime,
            "trade_count": len(self.trade_log),
            "buckets": {str(k): v for k, v in top},
        }

    def import_state(self, data: Dict[str, Any]):
        if not data or not isinstance(data, dict):
            return
        if "min_score" in data:
            self.current_min_score = _clamp(data["min_score"], 0.46, 0.70)
        if "regime" in data and data["regime"] in ("normal", "favorable", "chaotic"):
            self.regime = data["regime"]
        # Buckets restored lightly; full history rebuilds from live trades
