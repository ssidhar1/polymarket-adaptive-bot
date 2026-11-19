"""
Adaptive learner with conservative sample-size controls.

Learns from realized, size-normalized returns. Small samples are shrunk toward
neutral and cannot flip the global regime or grant setup-bucket bonuses.
"""
from collections import deque, defaultdict
from typing import Dict, Deque, Any, Tuple
import ast
import logging
import math
import numpy as np

logger = logging.getLogger("astf.learning")


def _clamp(x: float, lo: float, hi: float) -> float:
    try:
        value = float(x)
        if not math.isfinite(value):
            return lo
        return max(lo, min(hi, value))
    except (TypeError, ValueError):
        return lo


def _wilson_bounds(wins: int, total: int, z: float = 1.645) -> Tuple[float, float]:
    """90% Wilson interval for a binomial win rate."""
    if total <= 0:
        return 0.0, 1.0
    p = wins / total
    z2 = z * z
    denom = 1.0 + z2 / total
    center = (p + z2 / (2.0 * total)) / denom
    margin = z * math.sqrt((p * (1.0 - p) / total) + z2 / (4.0 * total * total)) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


class AdaptiveLearner:
    def __init__(self, config: dict):
        learning = config.get("learning", {}) or {}
        signals = config.get("signals", {}) or {}
        self.window = int(learning.get("calibration_window", 150) or 150)
        self.regime_lookback = int(learning.get("regime_lookback", 40) or 40)
        self.min_regime_trades = max(20, int(learning.get("min_regime_trades", 25) or 25))
        self.min_bucket_trades = max(15, int(learning.get("min_bucket_trades", 25) or 25))
        self.default_min = _clamp(signals.get("min_score", 0.54), 0.40, 0.80)
        self.trade_log: Deque[Dict] = deque(maxlen=max(30, self.window))
        self.current_min_score = float(self.default_min)
        self.regime = "normal"
        self._idle_cycles = 0
        self.buckets: Dict[Tuple[int, str, str], list] = defaultdict(lambda: [0, 0, 0.0])

    @staticmethod
    def _normalized_return(trade: Dict) -> float:
        pnl = _clamp(trade.get("pnl", 0.0), -1e9, 1e9)
        notional = _clamp(trade.get("size_usd", 0.0), 0.0, 1e12)
        # Missing notional is not comparable; do not silently treat raw USD
        # P&L as a return percentage.
        return pnl / notional if notional > 0 else 0.0

    def _bucket_key(self, trade: Dict) -> Tuple[int, str, str]:
        hour = int(trade.get("hour_utc") or 0)
        rem = float(trade.get("time_remaining_s") or 300)
        rem_bucket = "late" if rem < 90 else ("mid" if rem < 180 else "early")
        strength = abs(float(trade.get("vs_open") or trade.get("momentum") or 0))
        open_bucket = "strong" if strength > 0.35 else ("med" if strength > 0.15 else "weak")
        return hour, rem_bucket, open_bucket

    def log_trade(self, trade: Dict):
        if not isinstance(trade, dict) or trade.get("stale"):
            return
        self.trade_log.append(trade)
        self._idle_cycles = 0
        key = self._bucket_key(trade)
        bucket = self.buckets[key]
        bucket[1] += 1
        normalized = self._normalized_return(trade)
        bucket[2] += normalized
        if normalized > 0:
            bucket[0] += 1
        if len(self.trade_log) >= self.min_regime_trades:
            self._adapt()

    def get_bucket_boost(self, hour: int, remaining_s: float, vs_open: float) -> float:
        rem_bucket = "late" if remaining_s < 90 else ("mid" if remaining_s < 180 else "early")
        strength = abs(vs_open)
        open_bucket = "strong" if strength > 0.35 else ("med" if strength > 0.15 else "weak")
        bucket = self.buckets.get((hour, rem_bucket, open_bucket))
        if not bucket or bucket[1] < self.min_bucket_trades:
            return 0.0

        wins, total, return_sum = bucket
        lower, upper = _wilson_bounds(int(wins), int(total))
        avg_return = float(return_sum) / max(1, int(total))
        # Require both statistical support and positive realized economics.
        if lower > 0.45 and avg_return > 0.001:
            return 0.025
        if upper < 0.55 and avg_return < -0.001:
            return -0.03
        return 0.0

    def on_cycle(self, opened: int = 0):
        if opened > 0:
            self._idle_cycles = 0
            return
        self._idle_cycles += 1
        if self._idle_cycles < 25 or self.current_min_score <= self.default_min + 0.015:
            return
        old = self.current_min_score
        self.current_min_score = _clamp(
            0.72 * self.current_min_score + 0.28 * self.default_min, 0.46, 0.70
        )
        if self.regime == "chaotic" and self.current_min_score < 0.62:
            self.regime = "normal"
        self._idle_cycles = 0
        logger.info(f"Idle decay | min_score {old:.3f} -> {self.current_min_score:.3f}")

    def _adapt(self):
        recent = [t for t in list(self.trade_log)[-self.regime_lookback:] if not t.get("stale")]
        if len(recent) < self.min_regime_trades:
            return
        returns = [self._normalized_return(t) for t in recent]
        returns = [r for r in returns if math.isfinite(r)]
        if len(returns) < self.min_regime_trades:
            return

        wins = sum(1 for value in returns if value > 0)
        win_rate = wins / len(returns)
        avg_return = float(np.mean(returns))
        return_vol = float(np.std(returns))
        lower, upper = _wilson_bounds(wins, len(returns))

        if return_vol > 0.025 and upper < 0.52:
            self.regime = "chaotic"
            self.current_min_score = min(0.70, self.current_min_score + 0.015)
        elif lower > 0.48 and avg_return > 0.002:
            self.regime = "favorable"
            self.current_min_score = max(0.48, self.current_min_score - 0.01)
        else:
            self.regime = "normal"
            self.current_min_score = 0.90 * self.current_min_score + 0.10 * self.default_min

        self.current_min_score = _clamp(self.current_min_score, 0.46, 0.70)
        logger.info(
            f"Adapted | regime={self.regime} | min_score={self.current_min_score:.3f} | "
            f"n={len(returns)} wr={win_rate:.1%} avg_return={avg_return:.3%} "
            f"return_vol={return_vol:.3%} buckets={len(self.buckets)}"
        )

    def get_min_score(self) -> float:
        return float(self.current_min_score)

    def get_regime(self) -> str:
        return self.regime

    def export_state(self) -> Dict[str, Any]:
        top = sorted(
            [(key, value) for key, value in self.buckets.items() if value[1] >= 4],
            key=lambda item: -item[1][1],
        )[:80]
        return {
            "learning_state_version": 2,
            "min_score": self.current_min_score,
            "regime": self.regime,
            "trade_count": len(self.trade_log),
            "buckets": {str(key): list(value) for key, value in top},
        }

    def import_state(self, data: Dict[str, Any]):
        if not isinstance(data, dict):
            return
        if "min_score" in data:
            self.current_min_score = _clamp(data["min_score"], 0.46, 0.70)
        if data.get("regime") in ("normal", "favorable", "chaotic"):
            self.regime = data["regime"]

        restored = 0
        # Version 1 buckets stored raw USD P&L; never mix them with the
        # version 2 size-normalized returns used by the current learner.
        raw_buckets = data.get("buckets", {}) if data.get("learning_state_version") == 2 else {}
        if isinstance(raw_buckets, dict):
            for raw_key, raw_value in raw_buckets.items():
                try:
                    key = ast.literal_eval(raw_key)
                    if (
                        not isinstance(key, tuple) or len(key) != 3
                        or not isinstance(raw_value, (list, tuple)) or len(raw_value) != 3
                    ):
                        continue
                    hour, rem_bucket, open_bucket = key
                    hour = int(hour)
                    if not (0 <= hour <= 23):
                        continue
                    if rem_bucket not in ("early", "mid", "late") or open_bucket not in ("weak", "med", "strong"):
                        continue
                    wins, total, return_sum = int(raw_value[0]), int(raw_value[1]), float(raw_value[2])
                    if total < 0 or wins < 0 or wins > total or not math.isfinite(return_sum):
                        continue
                    self.buckets[(hour, rem_bucket, open_bucket)] = [wins, total, return_sum]
                    restored += 1
                except (ValueError, TypeError, SyntaxError):
                    continue
        if restored:
            logger.info(f"Restored {restored} adaptive setup buckets")
