"""
Advanced signal ensemble + structural edge detectors.
TWAP-lock (late vs_open), cross-asset lag, maker-rebate adjusted edge.
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
        self.min_score = float(sig.get("min_score", 0.54))
        self.min_edge = float(risk.get("min_edge_after_fees", 0.018))
        self.fee_rate = float(sig.get("fee_rate", 0.07))
        self.fee_buffer = 0.012
        self.min_depth_usd = float(sig.get("min_depth_usd", 12.0))
        self.min_time_remaining = float(sig.get("min_time_remaining_pct", 0.07))
        self.max_entry = float(sig.get("max_entry_price", 0.55))
        self.min_abs_momentum = float(sig.get("min_abs_momentum", 0.10))
        self.max_prob_lift = float(sig.get("max_prob_lift", 0.12))
        self.degen = bool(sig.get("degen_mode", True))
        self.prefer_maker = bool(config.get("execution", {}).get("prefer_maker", True))
        self.maker_rebate = 0.20  # 20% of fees returned to makers
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

    def _effective_fee(self, entry: float, fee_rate: float) -> float:
        fee = taker_fee_rate(entry, rate=fee_rate)
        if self.prefer_maker:
            fee *= (1.0 - self.maker_rebate)
        return fee

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
        fee = self._effective_fee(up_px, self.fee_rate) + self._effective_fee(down_px, self.fee_rate)
        edge = 1.0 - total - fee - 0.003
        min_edge = 0.005 if self.degen else 0.009
        if edge > min_edge:
            return {
                "type": "complete_set",
                "edge": edge,
                "up_price": up_px,
                "down_price": down_px,
                "score": 0.94 + min(0.05, edge * 6),
                "direction": "both",
                "est_prob": None,
                "pass": True,
            }
        return None

    def directional_score(self, features: Dict, preferred_side: Optional[str] = None) -> Dict:
        mom = float(features.get("momentum", 0.0) or 0.0)
        vs_open = float(features.get("vs_open", 0.0) or 0.0)
        other_vs_open = float(features.get("other_vs_open", 0.0) or 0.0)
        imb = float(features.get("book_imbalance", 0.0) or 0.0)
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
        time_left = float(features.get("time_remaining_pct", 0.5) or 0.5)

        if depth < self.min_depth_usd:
            return self._reject("thin_book")
        if remaining_s > window_s + 25:
            return self._reject("too_early")
        if remaining_s < 35:
            return self._reject("near_expiry")
        if time_left < self.min_time_remaining:
            return self._reject("near_expiry")
        if entry < 0.03 or entry > 0.97:
            return self._reject("extreme_mid")
        if entry > self.max_entry:
            return self._reject("expensive_entry")

        if preferred_side in ("up", "down"):
            direction = preferred_side
        else:
            direction = "up" if (mom + 0.65 * vs_open + 0.35 * imb) >= 0 else "down"

        signed_mom = mom if direction == "up" else -mom
        signed_open = vs_open if direction == "up" else -vs_open
        signed_other = other_vs_open if direction == "up" else -other_vs_open
        if feat_side in ("up", "down") and feat_side == direction:
            signed_imb = imb
        else:
            signed_imb = imb if direction == "up" else -imb

        # TWAP-lock: near end, vs_open dominates (resolution is TWAP)
        open_w = 0.30 if remaining_s > 200 else (0.62 if remaining_s < 120 else 0.48)
        mom_w = 1.0 - open_w
        directional_drive = mom_w * signed_mom + open_w * signed_open

        # Cross-asset lag boost: if the other asset is already moving strongly in same direction
        lag_boost = 0.0
        if abs(signed_other) > 0.25 and abs(signed_open) < abs(signed_other) * 0.6:
            lag_boost = 0.08 * signed_other * (1.0 if self.degen else 0.6)

        if directional_drive + lag_boost * 0.5 < self.min_abs_momentum * 0.35:
            return self._reject("against_mom")

        if abs(directional_drive) < self.min_abs_momentum * 0.8 and abs(signed_imb) < 0.09 and abs(lag_boost) < 0.04:
            return self._reject("weak_signal")

        time_term = 1.0 - abs(time_left - 0.30)
        cheap_term = max(0.0, (0.52 - entry) / 0.52)

        # TWAP-lock boost: strong late move that hasn't been fully priced
        twap_lock = 0.0
        if remaining_s <= 140 and directional_drive > 0.22 and entry < 0.58:
            twap_lock = 0.15 * directional_drive * (1.3 if remaining_s < 90 else 1.0)

        raw = (
            0.42 * signed_mom
            + 0.32 * signed_open
            + 0.12 * signed_imb
            + 0.06 * time_term
            + 0.10 * cheap_term
            + lag_boost
            + twap_lock
        )

        if self.degen:
            raw += 0.03 * max(0.0, directional_drive + lag_boost)

        if vol == "high":
            raw *= 0.86
        elif vol == "low":
            raw *= 1.06

        if spread > 0.06:
            raw *= 0.75
        elif spread > 0.035:
            raw *= 0.86

        score = float(np.clip(0.48 + raw * 0.60, 0.0, 1.0))

        drive_s = max(0.0, directional_drive + lag_boost * 0.7)
        imb_s = max(0.0, signed_imb)
        agree = 1.0 if (directional_drive > 0.04 and signed_imb > -0.05) else 0.45
        signal_strength = float(np.clip(0.70 * drive_s + 0.30 * imb_s, 0.0, 1.0)) * agree
        cheap_boost = 1.0 + 0.25 * cheap_term
        max_lift = self.max_prob_lift * (1.15 if self.degen else 1.0)
        lift = min(signal_strength * max_lift * cheap_boost, max_lift)
        est_prob = float(np.clip(entry + lift, 0.03, 0.97))

        raw_edge = est_prob - entry - self._effective_fee(entry, fee_rate)
        min_edge = self.min_edge if not self.degen else max(0.010, self.min_edge * 0.65)
        if raw_edge < min_edge:
            return self._reject("neg_ev")

        hard_pass = (
            score >= self.min_score
            and raw_edge >= min_edge
            and (directional_drive + lag_boost * 0.5) >= self.min_abs_momentum * 0.75
            and entry <= self.max_entry
        )

        return {
            "type": "directional",
            "score": score,
            "direction": direction,
            "est_prob": est_prob,
            "edge_after_fees": raw_edge,
            "entry": entry,
            "pass": hard_pass,
            "components": {
                "momentum": signed_mom,
                "vs_open": signed_open,
                "other_vs_open": signed_other,
                "drive": directional_drive,
                "lag_boost": lag_boost,
                "twap_lock": twap_lock,
                "imbalance": signed_imb,
                "edge": raw_edge,
                "effective_fee": self._effective_fee(entry, fee_rate),
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
