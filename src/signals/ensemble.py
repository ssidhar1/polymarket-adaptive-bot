"""
Advanced signal ensemble + structural edge detectors.
"""
from typing import Dict, Optional
import numpy as np
import logging

logger = logging.getLogger("astf.ensemble")


class EnsembleSignal:
    def __init__(self, config: dict):
        sig = config.get("signals", {})
        risk = config.get("risk", {})
        self.min_score = sig.get("min_score", 0.55)
        self.min_edge = risk.get("min_edge_after_fees", 0.012)
        self.fee_buffer = 0.02
        self.min_depth_usd = sig.get("min_depth_usd", 25.0)
        self.min_time_remaining = sig.get("min_time_remaining_pct", 0.10)
        self.max_time_remaining = sig.get("max_time_remaining_pct", 0.95)
        self.degen = bool(sig.get("degen_mode", True))
        self.reject_counts: Dict[str, int] = {}

    def _reject(self, reason: str) -> Dict:
        self.reject_counts[reason] = self.reject_counts.get(reason, 0) + 1
        return {
            "type": "directional",
            "score": 0.0,
            "direction": "up",
            "est_prob": 0.5,
            "pass": False,
            "reason": reason,
            "components": {},
        }

    def pop_reject_summary(self) -> Dict[str, int]:
        out = dict(self.reject_counts)
        self.reject_counts.clear()
        return out

    def complete_set_arb(
        self,
        up_mid: float,
        down_mid: float,
        up_ask: Optional[float] = None,
        down_ask: Optional[float] = None,
    ) -> Optional[Dict]:
        # Prefer asks for realistic buy cost
        up_px = up_ask if up_ask is not None else up_mid
        down_px = down_ask if down_ask is not None else down_mid
        if up_px is None or down_px is None:
            return None
        if not (0.02 < up_px < 0.98 and 0.02 < down_px < 0.98):
            return None
        total = up_px + down_px
        edge = 1.0 - total - self.fee_buffer
        min_edge = 0.006 if self.degen else 0.008
        if edge > min_edge:
            return {
                "type": "complete_set",
                "edge": edge,
                "up_price": up_px,
                "down_price": down_px,
                "score": 0.9 + min(0.09, edge * 5),
                "direction": "both",
                "est_prob": None,
                "pass": True,
            }
        return None

    def directional_score(self, features: Dict, preferred_side: Optional[str] = None) -> Dict:
        mom = float(features.get("momentum", 0.0) or 0.0)
        imb = float(features.get("book_imbalance", 0.0) or 0.0)
        time_left = float(features.get("time_remaining_pct", 0.5) or 0.5)
        remaining_s = float(features.get("time_remaining_s", 300) or 300)
        vol = features.get("volatility_regime", "normal")
        mid = features.get("mid_price")
        mid = 0.5 if mid is None else float(mid)
        spread = float(features.get("spread", 0.02) or 0.02)
        depth = float(features.get("depth_usd", 0.0) or 0.0)
        window_s = int(features.get("window_seconds", 900) or 900)

        if depth < self.min_depth_usd:
            return self._reject("thin_book")

        if time_left < self.min_time_remaining or remaining_s < 45:
            return self._reject("near_expiry")

        # Too early in a brand-new window: wait for book to form
        if time_left > self.max_time_remaining:
            return self._reject("too_early")

        if mid < 0.06 or mid > 0.94:
            return self._reject("extreme_mid")

        # Direction from momentum + imbalance
        lean = mom + 0.7 * imb
        if preferred_side in ("up", "down"):
            direction = preferred_side
        else:
            direction = "up" if lean >= 0 else "down"

        # For DOWN trades, invert momentum contribution so "price falling" is positive signal
        signed_mom = mom if direction == "up" else -mom
        signed_imb = imb if direction == "up" else -imb

        # Short windows weight short TF harder (mom already is MTF combined)
        short_w = 0.50 if window_s <= 360 else 0.38
        imb_w = 0.28 if window_s <= 360 else 0.32

        # Prefer mid-window and mid-range prices (more room for edge)
        time_term = 1.0 - abs(time_left - 0.45)
        mid_term = 1.0 - abs(mid - 0.5) * 1.6  # peak near 0.5

        raw = (
            short_w * signed_mom
            + imb_w * signed_imb
            + 0.14 * time_term
            + 0.10 * mid_term
        )

        # Bias toward actionable range when degen
        if self.degen:
            raw += 0.04 * abs(signed_mom)

        if vol == "high":
            raw *= 0.85
        elif vol == "low":
            raw *= 1.06

        if spread > 0.05:
            raw *= 0.80
        elif spread > 0.03:
            raw *= 0.90

        # Map raw roughly from [-1,1] toward [0,1] score
        score = float(np.clip(0.5 + raw * 0.55, 0.0, 1.0))

        strength = abs(score - 0.5) * 1.5
        est_prob = float(np.clip(0.5 + strength, 0.05, 0.95))

        # Edge vs market price of chosen side
        edge = est_prob - mid
        if edge < (0.01 if self.degen else 0.015):
            # still allow high-score passes through min_score gate
            pass

        return {
            "type": "directional",
            "score": score,
            "direction": direction,
            "est_prob": est_prob,
            "pass": score >= self.min_score,
            "components": {
                "momentum": signed_mom,
                "imbalance": signed_imb,
                "time": time_left,
                "vol": vol,
                "spread": spread,
                "depth": depth,
                "raw": raw,
            },
        }

    def evaluate(
        self,
        features: Dict,
        up_mid: Optional[float] = None,
        down_mid: Optional[float] = None,
        up_ask: Optional[float] = None,
        down_ask: Optional[float] = None,
    ) -> Dict:
        arb = self.complete_set_arb(up_mid, down_mid, up_ask=up_ask, down_ask=down_ask)
        if arb:
            return arb
        return self.directional_score(features)
