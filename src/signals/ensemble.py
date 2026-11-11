"""
Advanced signal ensemble + structural edge detectors.

Includes:
- Multi-feature directional score
- Complete-set arbitrage (Up + Down < 1.0 after costs)
- Time-decay urgency
- Simple calibrated probability estimate
"""
from typing import Dict, Optional, Tuple
import numpy as np
import logging

logger = logging.getLogger("astf.ensemble")

class EnsembleSignal:
    def __init__(self, config: dict):
        self.min_score = config.get("signals", {}).get("min_score", 0.62)
        self.min_edge = config.get("risk", {}).get("min_edge_after_fees", 0.015)
        self.fee_buffer = 0.02  # rough taker fee + slippage cushion

    def complete_set_arb(self, up_mid: float, down_mid: float) -> Optional[Dict]:
        """
        Classic structural edge: if Up + Down < 1.0 - costs, buy both.
        Returns action dict or None.
        """
        if up_mid is None or down_mid is None:
            return None
        total = up_mid + down_mid
        edge = 1.0 - total - self.fee_buffer
        if edge > 0.008:  # meaningful after costs
            return {
                "type": "complete_set",
                "edge": edge,
                "up_price": up_mid,
                "down_price": down_mid,
                "score": 0.9 + min(0.09, edge * 5),  # high confidence structural
                "direction": "both",
            }
        return None

    def directional_score(self, features: Dict) -> Dict:
        mom = features.get("momentum", 0.0)
        imb = features.get("book_imbalance", 0.0)
        time_left = features.get("time_remaining_pct", 0.5)
        vol = features.get("volatility_regime", "normal")
        mid = features.get("mid_price") or 0.5
        spread = features.get("spread", 0.02)

        # Core weighted score
        raw = (
            0.38 * mom +
            0.32 * imb +
            0.18 * (1.0 - abs(time_left - 0.25)) +  # prefer mid-window
            0.12 * (0.5 - abs(mid - 0.5)) * 2       # prefer clearer prices
        )

        # Regime adjustments
        if vol == "high":
            raw *= 0.82
        elif vol == "low":
            raw *= 1.08

        # Spread penalty (wide books = worse fills)
        if spread > 0.04:
            raw *= 0.85

        score = float(np.clip(raw, 0.0, 1.0))
        direction = "up" if (mom + imb) > 0 else "down"

        # Simple calibrated prob estimate (score mapped toward probability)
        # This is a rough proxy, not a true probability model
        est_prob = 0.5 + (score - 0.5) * 0.7
        est_prob = float(np.clip(est_prob, 0.05, 0.95))

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
            },
        }

    def evaluate(self, features: Dict, up_mid: Optional[float] = None, down_mid: Optional[float] = None) -> Dict:
        # Prefer structural arb when available
        arb = self.complete_set_arb(up_mid, down_mid)
        if arb:
            return arb

        return self.directional_score(features)
