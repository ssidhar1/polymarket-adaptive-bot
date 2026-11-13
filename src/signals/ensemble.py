"""
Advanced signal ensemble + structural edge detectors.
Profitability-first: reject negative EV after fees, prefer cheap shares.
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
        self.min_edge = risk.get("min_edge_after_fees", 0.02)
        self.fee_rate = float(sig.get("fee_rate", 0.01))
        self.fee_buffer = self.fee_rate * 2
        self.min_depth_usd = sig.get("min_depth_usd", 25.0)
        self.min_time_remaining = sig.get("min_time_remaining_pct", 0.10)
        # Optional soft preference — no longer a hard block at 0.90
        self.max_time_remaining = sig.get("max_time_remaining_pct", 0.98)
        self.max_entry = float(sig.get("max_entry_price", 0.58))
        self.min_abs_momentum = float(sig.get("min_abs_momentum", 0.12))
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
        up_px = up_ask if up_ask is not None else up_mid
        down_px = down_ask if down_ask is not None else down_mid
        if up_px is None or down_px is None:
            return None
        if not (0.02 < up_px < 0.98 and 0.02 < down_px < 0.98):
            return None
        total = up_px + down_px
        edge = 1.0 - total - self.fee_buffer
        min_edge = 0.01 if self.degen else 0.015
        if edge > min_edge:
            return {
                "type": "complete_set",
                "edge": edge,
                "up_price": up_px,
                "down_price": down_px,
                "score": 0.92 + min(0.07, edge * 4),
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
        ask = features.get("ask_price")
        entry = float(ask if ask is not None else mid)
        spread = float(features.get("spread", 0.02) or 0.02)
        depth = float(features.get("depth_usd", 0.0) or 0.0)
        window_s = int(features.get("window_seconds", 900) or 900)

        if depth < self.min_depth_usd:
            return self._reject("thin_book")

        # Not started yet: remaining longer than the window length
        if remaining_s > window_s + 15:
            return self._reject("too_early")

        if time_left < self.min_time_remaining or remaining_s < 60:
            return self._reject("near_expiry")

        if entry < 0.05 or entry > 0.95:
            return self._reject("extreme_mid")

        if entry > self.max_entry:
            return self._reject("expensive_entry")

        if preferred_side in ("up", "down"):
            direction = preferred_side
        else:
            direction = "up" if (mom + 0.7 * imb) >= 0 else "down"

        signed_mom = mom if direction == "up" else -mom
        signed_imb = imb if direction == "up" else -imb

        if abs(signed_mom) < self.min_abs_momentum and abs(signed_imb) < 0.15:
            return self._reject("weak_signal")

        short_w = 0.52 if window_s <= 360 else 0.40
        imb_w = 0.26 if window_s <= 360 else 0.30

        time_term = 1.0 - abs(time_left - 0.40)
        cheap_term = max(0.0, (0.55 - entry) / 0.55)

        raw = (
            short_w * signed_mom
            + imb_w * signed_imb
            + 0.12 * time_term
            + 0.10 * cheap_term
        )

        if self.degen:
            raw += 0.03 * max(0.0, signed_mom)

        if vol == "high":
            raw *= 0.88
        elif vol == "low":
            raw *= 1.05

        if spread > 0.05:
            raw *= 0.82
        elif spread > 0.03:
            raw *= 0.92

        score = float(np.clip(0.5 + raw * 0.55, 0.0, 1.0))

        signal_strength = max(0.0, 0.6 * signed_mom + 0.4 * signed_imb)
        signal_strength = float(np.clip(signal_strength, 0.0, 1.0))
        max_lift = 0.08 if self.degen else 0.06
        lift = signal_strength * max_lift
        est_prob = float(np.clip(entry + lift, 0.05, 0.95))

        edge_after_fees = est_prob - entry - self.fee_rate
        min_edge = self.min_edge if not self.degen else max(0.015, self.min_edge * 0.75)
        if edge_after_fees < min_edge:
            return self._reject("neg_ev")

        return {
            "type": "directional",
            "score": score,
            "direction": direction,
            "est_prob": est_prob,
            "edge_after_fees": edge_after_fees,
            "entry": entry,
            "pass": score >= self.min_score and edge_after_fees >= min_edge,
            "components": {
                "momentum": signed_mom,
                "imbalance": signed_imb,
                "time": time_left,
                "vol": vol,
                "spread": spread,
                "depth": depth,
                "raw": raw,
                "lift": lift,
                "edge": edge_after_fees,
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
