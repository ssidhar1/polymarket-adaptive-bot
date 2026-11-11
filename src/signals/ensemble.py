"""
Advanced signal ensemble + structural edge detectors.

- Multi-feature directional score
- Complete-set arbitrage (Up + Down < 1.0 after costs)
- Liquidity / depth filter
- Simple calibrated probability estimate
"""
from typing import Dict, Optional
import numpy as np
import logging

logger = logging.getLogger("astf.ensemble")


class EnsembleSignal:
    def __init__(self, config: dict):
        self.min_score = config.get("signals", {}).get("min_score", 0.62)
        self.min_edge = config.get("risk", {}).get("min_edge_after_fees", 0.015)
        self.fee_buffer = 0.02
        self.min_depth_usd = config.get("signals", {}).get("min_depth_usd", 50.0)

    def complete_set_arb(self, up_mid: float, down_mid: float) -> Optional[Dict]:
        if up_mid is None or down_mid is None:
            return None
        if not (0.01 < up_mid < 0.99 and 0.01 < down_mid < 0.99):
            return None
        total = up_mid + down_mid
        edge = 1.0 - total - self.fee_buffer
        if edge > 0.008:
            return {
                "type": "complete_set",
                "edge": edge,
                "up_price": up_mid,
                "down_price": down_mid,
                "score": 0.9 + min(0.09, edge * 5),
                "direction": "both",
                "est_prob": None,
                "pass": True,
            }
        return None

    def directional_score(self, features: Dict) -> Dict:
        mom = float(features.get("momentum", 0.0) or 0.0)
        imb = float(features.get("book_imbalance", 0.0) or 0.0)
        time_left = float(features.get("time_remaining_pct", 0.5) or 0.5)
        vol = features.get("volatility_regime", "normal")
        mid = features.get("mid_price")
        if mid is None:
            mid = 0.5
        mid = float(mid)
        spread = float(features.get("spread", 0.02) or 0.02)
        depth = float(features.get("depth_usd", 0.0) or 0.0)

        # Thin book → reject
        if depth < self.min_depth_usd:
            return {
                "type": "directional",
                "score": 0.0,
                "direction": "up",
                "est_prob": 0.5,
                "pass": False,
                "reason": "thin_book",
                "components": {},
            }

        raw = (
            0.38 * mom
            + 0.32 * imb
            + 0.18 * (1.0 - abs(time_left - 0.25))
            + 0.12 * (0.5 - abs(mid - 0.5)) * 2
        )

        if vol == "high":
            raw *= 0.82
        elif vol == "low":
            raw *= 1.08

        if spread > 0.04:
            raw *= 0.85

        score = float(np.clip(raw, 0.0, 1.0))
        direction = "up" if (mom + imb) >= 0 else "down"

        # Map score toward a soft probability (proxy only)
        est_prob = 0.5 + (score - 0.5) * 0.7
        est_prob = float(np.clip(est_prob, 0.05, 0.95))
        if direction == "down":
            # est_prob is P(up-like strength); invert for down side pricing vs mid of UP token
            # When we buy down, we care about P(down). Keep est_prob as confidence on chosen side.
            pass

        return {
            "type": "directional",
            "score": score,
            "direction": direction,
            "est_prob": est_prob,
            "pass": score >= self.min_score,
            "components": {
                "momentum": mom,
                "imbalance": imb,
                "time": time_left,
                "vol": vol,
                "spread": spread,
                "depth": depth,
            },
        }

    def evaluate(
        self,
        features: Dict,
        up_mid: Optional[float] = None,
        down_mid: Optional[float] = None,
    ) -> Dict:
        arb = self.complete_set_arb(up_mid, down_mid)
        if arb:
            return arb
        return self.directional_score(features)
