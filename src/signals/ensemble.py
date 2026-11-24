"""
Advanced signal ensemble.
TWAP-lock / near-resolution sleeve, cross-asset lag, cross-window lead-lag,
microprice, calibrated probability support, complete-set arb.
"""
from typing import Dict, Optional
import numpy as np
import logging

from src.utils.fees import taker_fee_rate

logger = logging.getLogger("astf.ensemble")


class EnsembleSignal:
    def __init__(self, config: dict):
        sig = config.get("signals", {})
        risk = config.get("risk", {})
        self.min_score = float(sig.get("min_score", 0.52))
        self.min_edge = float(risk.get("min_edge_after_fees", 0.015))
        self.fee_rate = float(sig.get("fee_rate", 0.07))
        self.paper_slippage_bps = max(0.0, float(config.get("execution", {}).get("paper_slippage_bps", 20)))
        self.min_depth_usd = float(sig.get("min_depth_usd", 10.0))
        self.min_time_remaining = float(sig.get("min_time_remaining_pct", 0.05))
        self.max_entry = float(sig.get("max_entry_price", 0.57))
        self.min_abs_momentum = float(sig.get("min_abs_momentum", 0.08))
        self.max_prob_lift = min(0.08, max(0.0, float(sig.get("max_prob_lift", 0.05))))
        self.degen = bool(sig.get("degen_mode", True))
        self.allow_heuristic_paper_entries = bool(sig.get("allow_heuristic_paper_entries", False))
        self.near_res_boost = bool(sig.get("near_resolution_boost", True))
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

    def _effective_fee(self, entry: float, fee_rate: float, fee_exponent: float = 1.0) -> float:
        return taker_fee_rate(entry, rate=fee_rate, exponent=fee_exponent) + entry * self.paper_slippage_bps / 10_000.0

    def complete_set_arb(self, up_mid, down_mid, up_ask=None, down_ask=None,
                         fee_rate=None, fee_exponent=1.0):
        if up_ask is None or down_ask is None:
            return None
        up_px, down_px = float(up_ask), float(down_ask)
        if not (0.02 < up_px < 0.98 and 0.02 < down_px < 0.98):
            return None
        total = up_px + down_px
        effective_rate = self.fee_rate if fee_rate is None else float(fee_rate)
        fee = self._effective_fee(up_px, effective_rate, fee_exponent) + self._effective_fee(down_px, effective_rate, fee_exponent)
        edge = 1.0 - total - fee - 0.0015
        min_edge = 0.003 if self.degen else 0.006
        if edge > min_edge:
            return {
                "type": "complete_set",
                "edge": edge,
                "up_price": up_px,
                "down_price": down_px,
                "score": 0.96 + min(0.03, edge * 10),
                "direction": "both",
                "est_prob": None,
                "pass": True,
            }
        return None

    def directional_score(self, features: Dict, preferred_side: Optional[str] = None,
                          calibrated_prob: Optional[float] = None) -> Dict:
        mom = float(features.get("momentum", 0.0) or 0.0)
        vs_open = float(features.get("vs_open", 0.0) or 0.0)
        other_vs_open = float(features.get("other_vs_open", 0.0) or 0.0)
        window_lead = float(features.get("window_lead", 0.0) or 0.0)
        imb = float(features.get("book_imbalance", 0.0) or 0.0)
        micro = float(features.get("microprice_delta", 0.0) or 0.0)
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
        fee_rate = float(features.get("fee_rate", self.fee_rate))
        fee_exponent = float(features.get("fee_exponent", 1.0))
        time_left = float(features.get("time_remaining_pct", 0.5) or 0.5)

        if depth < self.min_depth_usd:
            return self._reject("thin_book")
        if remaining_s > window_s + 30:
            return self._reject("too_early")
        if remaining_s < 25:
            return self._reject("near_expiry")
        if time_left < self.min_time_remaining:
            return self._reject("near_expiry")
        if entry < 0.02 or entry > 0.98:
            return self._reject("extreme_mid")
        if entry > self.max_entry:
            return self._reject("expensive_entry")

        if preferred_side in ("up", "down"):
            direction = preferred_side
        else:
            direction = "up" if (mom + 0.7 * vs_open + 0.3 * imb) >= 0 else "down"

        signed_mom = mom if direction == "up" else -mom
        signed_open = vs_open if direction == "up" else -vs_open
        signed_other = other_vs_open if direction == "up" else -other_vs_open
        signed_lead = window_lead if direction == "up" else -window_lead
        if feat_side in ("up", "down") and feat_side == direction:
            signed_imb = imb
        else:
            signed_imb = imb if direction == "up" else -imb

        # Near-resolution / TWAP-lock weighting
        open_w = 0.22 if remaining_s > 200 else (0.75 if remaining_s < 90 else 0.48)
        directional_drive = (1 - open_w) * signed_mom + open_w * signed_open

        lag_boost = 0.0
        if abs(signed_other) > 0.25 and abs(signed_open) < abs(signed_other) * 0.5:
            lag_boost = 0.11 * signed_other * (1.15 if self.degen else 0.7)

        lead_boost = 0.0
        if window_s >= 800 and abs(signed_lead) > 0.32:
            lead_boost = 0.13 * signed_lead * (1.2 if self.degen else 0.8)

        signed_micro = micro
        micro_boost = float(np.clip(signed_micro * 0.22, -0.07, 0.07))
        total_drive = directional_drive + lag_boost * 0.65 + lead_boost * 0.55 + micro_boost

        # 5m windows have been more profitable; require stronger drive on 15m
        drive_floor = self.min_abs_momentum * (0.40 if window_s >= 800 else 0.22)
        if total_drive < drive_floor:
            return self._reject("against_mom")

        if abs(total_drive) < self.min_abs_momentum * 0.65 and abs(signed_imb) < 0.07:
            return self._reject("weak_signal")

        time_term = 1.0 - abs(time_left - 0.25)
        cheap_term = max(0.0, (0.58 - entry) / 0.58)

        # Near-resolution sleeve boost
        near_res = 0.0
        # Late-window "buy the winner" style boost once time is short and drive is clear
        if self.near_res_boost and remaining_s <= 90 and total_drive > 0.15 and entry < 0.65:
            near_res = 0.28 * total_drive * (1.6 if remaining_s < 50 else 1.2)

        raw = (
            0.35 * signed_mom
            + 0.38 * signed_open
            + 0.07 * signed_imb
            + 0.04 * float(np.clip(signed_micro, -1.0, 1.0))
            + 0.04 * time_term
            + 0.07 * cheap_term
            + lag_boost
            + lead_boost
            + near_res
        )

        if self.degen:
            raw += 0.04 * max(0.0, total_drive)

        if vol == "high":
            raw *= 0.82
        elif vol == "low":
            raw *= 1.08

        if spread > 0.07:
            raw *= 0.70
        elif spread > 0.04:
            raw *= 0.82

        score = float(np.clip(0.44 + raw * 0.65, 0.0, 1.0))

        # Probability: prefer calibrated when available
        if calibrated_prob is not None and 0.05 < calibrated_prob < 0.95:
            est_prob = float(calibrated_prob)
            prob_kind = "calibrated"
        else:
            drive_s = max(0.0, total_drive)
            imb_s = max(0.0, signed_imb)
            agree = 1.0 if (total_drive > 0.02 and signed_imb > -0.1) else 0.4
            signal_strength = float(np.clip(0.75 * drive_s + 0.25 * imb_s, 0.0, 1.0)) * agree
            cheap_boost = 1.0 + 0.3 * cheap_term
            max_lift = self.max_prob_lift * (1.2 if self.degen else 1.0)
            lift = min(signal_strength * max_lift * cheap_boost, max_lift)
            est_prob = float(np.clip(entry + lift, 0.02, 0.98))
            prob_kind = "uncalibrated_heuristic_proxy"

        heuristic_edge = est_prob - entry - self._effective_fee(entry, fee_rate, fee_exponent)
        min_edge = self.min_edge if not self.degen else max(0.007, self.min_edge * 0.55)
        if heuristic_edge < min_edge:
            return self._reject("heuristic_edge_below_costs")

        signal_pass = (
            score >= self.min_score
            and heuristic_edge >= min_edge
            and total_drive >= self.min_abs_momentum * 0.6
            and entry <= self.max_entry
        )

        # Near-resolution can override the calibration gate slightly when very strong
        near_res_override = (
            self.near_res_boost
            and remaining_s < 85
            and total_drive > 0.25
            and heuristic_edge > min_edge * 1.3
            and entry < 0.55
        )

        if not signal_pass and not near_res_override:
            return self._reject("signal_threshold")

        if not self.allow_heuristic_paper_entries and prob_kind != "calibrated" and not near_res_override:
            self.reject_counts["uncalibrated_probability"] = self.reject_counts.get("uncalibrated_probability", 0) + 1
            return {
                "type": "directional",
                "score": score,
                "direction": direction,
                "est_prob": None,
                "edge_after_fees": None,
                "heuristic_probability_proxy": est_prob,
                "heuristic_edge_proxy": heuristic_edge,
                "probability_kind": prob_kind,
                "entry": entry,
                "pass": False,
                "reason": "uncalibrated_probability",
                "near_resolution": near_res > 0,
                "components": {
                    "drive": total_drive,
                    "near_res": near_res,
                    "heuristic_edge_proxy": heuristic_edge,
                },
            }

        return {
            "type": "directional",
            "score": score,
            "direction": direction,
            "est_prob": est_prob,
            "edge_after_fees": heuristic_edge,
            "heuristic_probability_proxy": est_prob,
            "heuristic_edge_proxy": heuristic_edge,
            "probability_kind": prob_kind,
            "entry": entry,
            "pass": True,
            "paper_probe": prob_kind != "calibrated",
            "near_resolution": near_res > 0 or near_res_override,
            "components": {
                "drive": total_drive,
                "near_res": near_res,
                "heuristic_edge_proxy": heuristic_edge,
                "effective_fee": self._effective_fee(entry, fee_rate, fee_exponent),
            },
        }

    def evaluate(self, features, up_mid=None, down_mid=None, up_ask=None, down_ask=None,
                 calibrated_prob=None):
        arb = self.complete_set_arb(up_mid, down_mid, up_ask, down_ask)
        if arb:
            return arb
        return self.directional_score(features, calibrated_prob=calibrated_prob)
