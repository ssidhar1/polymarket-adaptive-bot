"""
Advanced signal ensemble + structural edge detectors.
Uses Polymarket-style dynamic taker fees and vs-open drift (proxy for Chainlink TWAP).
"""
from typing import Dict, Optional
import numpy as np
import logging

from src.utils.fees import edge_after_taker_fee, taker_fee_rate

logger = logging.getLogger("astf.ensemble")


class EnsembleSignal:
    def __init__(self, config: dict):
        sig = config.get("signals", {})
        risk = config.get("risk", {})
        self.min_score = sig.get("min_score", 0.54)
        self.min_edge = risk.get("min_edge_after_fees", 0.018)
        self.fee_rate = float(sig.get("fee_rate", 0.07))
        self.fee_buffer = 0.015
        self.min_depth_usd = sig.get("min_depth_usd", 15.0)
        self.min_time_remaining = sig.get("min_time_remaining_pct", 0.08)
        self.max_time_remaining = sig.get("max_time_remaining_pct", 0.98)
        self.max_entry = float(sig.get("max_entry_price", 0.54))
        self.min_abs_momentum = float(sig.get("min_abs_momentum", 0.11))
        self.max_prob_lift = float(sig.get("max_prob_lift", 0.11))
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
        fee = taker_fee_rate(up_px, rate=self.fee_rate) + taker_fee_rate(down_px, rate=self.fee_rate)
        edge = 1.0 - total - fee - 0.004
        min_edge = 0.006 if self.degen else 0.010
        if edge > min_edge:
            return {
                "type": "complete_set",
                "edge": edge,
                "up_price": up_px,
                "down_price": down_px,
                "score": 0.93 + min(0.06, edge * 5),
                "direction": "both",
                "est_prob": None,
                "pass": True,
            }
        return None

    def directional_score(self, features: Dict, preferred_side: Optional[str] = None) -> Dict:
        mom = float(features.get("momentum", 0.0) or 0.0)
        vs_open = float(features.get("vs_open", 0.0) or 0.0)
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
        feat_side = str(features.get("side") or "").lower()
        fee_rate = float(features.get("fee_rate") or self.fee_rate)

        if depth < self.min_depth_usd:
            return self._reject("thin_book")

        if remaining_s > window_s + 20:
            return self._reject("too_early")

        if remaining_s < 40:
            return self._reject("near_expiry")
        if time_left < self.min_time_remaining:
            return self._reject("near_expiry")

        if entry < 0.04 or entry > 0.96:
            return self._reject("extreme_mid")

        if entry > self.max_entry:
            return self._reject("expensive_entry")

        if preferred_side in ("up", "down"):
            direction = preferred_side
        else:
            direction = "up" if (mom + 0.6 * vs_open + 0.4 * imb) >= 0 else "down"

        signed_mom = mom if direction == "up" else -mom
        signed_open = vs_open if direction == "up" else -vs_open
        if feat_side in ("up", "down") and feat_side == direction:
            signed_imb = imb
        else:
            signed_imb = imb if direction == "up" else -imb

        # vs_open gets higher weight late in the window (closer to Chainlink TWAP resolution)
        open_weight = 0.35 if remaining_s > 180 else 0.55
        mom_weight = 1.0 - open_weight
        directional_drive = mom_weight * signed_mom + open_weight * signed_open

        if directional_drive < self.min_abs_momentum * 0.40:
            return self._reject("against_mom")

        if abs(directional_drive) < self.min_abs_momentum * 0.85 and abs(signed_imb) < 0.10:
            return self._reject("weak_signal")

        short_w = 0.48 if window_s <= 360 else 0.38
        open_w = 0.28 if remaining_s < 150 else 0.18
        imb_w = 0.14 if window_s <= 360 else 0.20

        time_term = 1.0 - abs(time_left - 0.32)
        cheap_term = max(0.0, (0.50 - entry) / 0.50)

        # Strong late-window boost when vs_open agrees and share is still cheap
        late_boost = 0.0
        if 45 <= remaining_s <= 160 and directional_drive > 0.18 and entry < 0.50:
            late_boost = 0.12 * directional_drive * (1.0 if remaining_s < 100 else 0.7)

        raw = (
            short_w * signed_mom
            + open_w * signed_open
            + imb_w * signed_imb
            + 0.07 * time_term
            + 0.13 * cheap_term
            + late_boost
        )

        if self.degen:
            raw += 0.025 * max(0.0, directional_drive)

        if vol == "high":
            raw *= 0.88
        elif vol == "low":
            raw *= 1.04

        if spread > 0.055:
            raw *= 0.78
        elif spread > 0.032:
            raw *= 0.88

        score = float(np.clip(0.5 + raw * 0.58, 0.0, 1.0))

        drive_s = max(0.0, directional_drive)
        imb_s = max(0.0, signed_imb)
        agree = 1.0 if (directional_drive > 0.05 and signed_imb > 0) else 0.48
        signal_strength = float(np.clip(0.68 * drive_s + 0.32 * imb_s, 0.0, 1.0)) * agree
        cheap_boost = 1.0 + 0.22 * cheap_term
        max_lift = self.max_prob_lift * (1.12 if self.degen else 1.0)
        lift = min(signal_strength * max_lift * cheap_boost, max_lift)
        est_prob = float(np.clip(entry + lift, 0.04, 0.96))

        edge_after_fees = edge_after_taker_fee(est_prob, entry, rate=fee_rate)
        min_edge = self.min_edge if not self.degen else max(0.012, self.min_edge * 0.72)
        if edge_after_fees < min_edge:
            return self._reject("neg_ev")

        hard_pass = (
            score >= self.min_score
            and edge_after_fees >= min_edge
            and directional_drive >= self.min_abs_momentum * 0.9
            and entry <= self.max_entry
        )

        return {
            "type": "directional",
            "score": score,
            "direction": direction,
            "est_prob": est_prob,
            "edge_after_fees": edge_after_fees,
            "entry": entry,
            "pass": hard_pass,
            "components": {
                "momentum": signed_mom,
                "vs_open": signed_open,
                "drive": directional_drive,
                "imbalance": signed_imb,
                "time": time_left,
                "vol": vol,
                "spread": spread,
                "depth": depth,
                "raw": raw,
                "lift": lift,
                "edge": edge_after_fees,
                "fee": taker_fee_rate(entry, rate=fee_rate),
                "late_boost": late_boost,
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
