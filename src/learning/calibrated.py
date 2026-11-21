"""Online calibrated probability estimator for short-term crypto markets.

Uses recent resolved trades to adjust raw heuristic lifts toward empirical
frequency. Falls back safely when sample size is small.
"""
from collections import deque
from typing import Dict, Optional, Deque
import math
import logging
import numpy as np

logger = logging.getLogger("astf.learning.calibrated")


def _clamp(x: float, lo: float, hi: float) -> float:
    try:
        v = float(x)
        if not math.isfinite(v):
            return lo
        return max(lo, min(hi, v))
    except (TypeError, ValueError):
        return lo


class CalibratedProbabilityModel:
    """Lightweight online calibrator.

    Tracks (feature_bucket -> win rate) and applies a shrinkage adjustment
    to the heuristic est_prob. Requires minimum samples before overriding.
    """

    def __init__(self, min_samples: int = 20, lookback: int = 300):
        self.min_samples = max(10, min_samples)
        self.lookback = max(50, lookback)
        self.history: Deque[Dict] = deque(maxlen=self.lookback)
        # bucket key: (hour_bucket, rem_bucket, strength_bucket) -> [wins, total]
        self.buckets: Dict[tuple, list] = {}

    def _bucket(self, hour: int, remaining_s: float, strength: float) -> tuple:
        h_b = "peak" if 13 <= hour <= 21 else "off"
        rem_b = "late" if remaining_s < 100 else ("mid" if remaining_s < 200 else "early")
        str_b = "strong" if abs(strength) > 0.3 else ("med" if abs(strength) > 0.12 else "weak")
        return (h_b, rem_b, str_b)

    def observe(self, trade: Dict):
        """Record a resolved trade for calibration."""
        if not isinstance(trade, dict) or trade.get("stale"):
            return
        pnl = float(trade.get("pnl") or 0)
        won = 1 if pnl > 0 else 0
        hour = int(trade.get("hour_utc") or 0)
        rem = float(trade.get("time_remaining_s") or 300)
        strength = float(trade.get("vs_open") or trade.get("momentum") or 0)
        key = self._bucket(hour, rem, strength)
        if key not in self.buckets:
            self.buckets[key] = [0, 0]
        self.buckets[key][1] += 1
        self.buckets[key][0] += won
        self.history.append({"won": won, "key": key})

    def calibrate(self, heuristic_prob: float, hour: int, remaining_s: float, strength: float) -> Optional[float]:
        """Return calibrated probability or None if insufficient data."""
        key = self._bucket(hour, remaining_s, strength)
        bucket = self.buckets.get(key)
        if not bucket or bucket[1] < self.min_samples:
            # Global fallback
            if len(self.history) < self.min_samples:
                return None
            global_wr = sum(h["won"] for h in self.history) / len(self.history)
            # Shrink heuristic toward global empirical
            return _clamp(0.6 * heuristic_prob + 0.4 * global_wr, 0.05, 0.95)

        wr = bucket[0] / bucket[1]
        # Shrink toward bucket empirical rate
        calibrated = 0.55 * heuristic_prob + 0.45 * wr
        return _clamp(calibrated, 0.05, 0.95)

    def export_state(self) -> Dict:
        return {
            "buckets": {str(k): v for k, v in self.buckets.items()},
            "history_len": len(self.history),
        }

    def import_state(self, data: Dict):
        if not isinstance(data, dict):
            return
        # Light restore; full history rebuilds from live trades
        raw = data.get("buckets", {})
        for k, v in raw.items():
            try:
                key = eval(k)  # safe in this controlled context
                if isinstance(key, tuple) and len(key) == 3 and isinstance(v, list) and len(v) == 2:
                    self.buckets[key] = v
            except Exception:
                continue
