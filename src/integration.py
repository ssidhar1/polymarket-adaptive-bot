"""Integration helpers for calibrated probability and complete-set execution.

Import and use these from the main loop.
"""
from datetime import datetime, timezone
from src.learning.calibrated import CalibratedProbabilityModel


def make_calibrator(min_samples: int = 15) -> CalibratedProbabilityModel:
    return CalibratedProbabilityModel(min_samples=min_samples)


def observe_resolve(calibrator: CalibratedProbabilityModel, trade: dict):
    if isinstance(trade, dict) and not trade.get("stale"):
        calibrator.observe(trade)


def get_calibrated_prob(calibrator: CalibratedProbabilityModel, feat: dict, vs_open: float) -> float | None:
    try:
        return calibrator.calibrate(
            heuristic_prob=0.52,
            hour=datetime.now(timezone.utc).hour,
            remaining_s=float(feat.get("time_remaining_s") or 300),
            strength=vs_open,
        )
    except Exception:
        return None


def should_execute_arb(config: dict, arb: dict) -> bool:
    if not arb:
        return False
    if not config.get("execution", {}).get("execute_complete_set", False):
        return False
    return float(arb.get("edge") or 0) > 0.003
