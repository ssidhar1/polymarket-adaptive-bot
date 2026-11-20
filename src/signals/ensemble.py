"""
Advanced signal ensemble.
TWAP-lock, cross-asset lag, cross-window lead-lag (5m → 15m), maker-rebate edge.
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
        self.max_prob_lift = min(0.06, max(0.0, float(sig.get("max_prob_lift", 0.04))))
        self.degen = bool(sig.get("degen_mode", True))
        # Technical feature lifts are not calibrated probabilities. Keep them
        # diagnostic-only unless an operator explicitly enables paper probes.
        self.allow_heuristic_paper_entries = bool(sig.get("allow_heuristic_paper_entries", False))
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
        # Aggressive entries pay full taker fees; no assumed maker credit.
        return taker_fee_rate(entry, rate=fee_rate) + entry * self.paper_slippage_bps / 10_000.0

    def complete_set_arb(self, up_mid, down_mid, up_ask=None, down_ask=None):
        up_px = up_ask if up_ask is not None else up_mid
        down_px = down_ask if down_ask is not None else down_mid
        if up_px is None or down_px is None:
            return None
        if not (0.02 < up_px < 0.98 and 0.02 < down_px < 0.98):
            return None
        total = up_px + down_px
        fee = self._effective_fee(up_px, self.fee_rate) + self._effective_fee(down_px, self.fee_rate)
        edge = 1.0 - total - fee - 0.002
        min_edge = 0.004 if self.degen else 0.008
        if edge > min_edge:
            return {
                "type": "complete_set",
                "edge": edge,
                "up_price": up_px,
                "down_price": down_px,
                "score": 0.95 + min(0.04, edge * 8),
                "direction": "both",
                "est_prob": None,
                "pass": True,
            }
        return None

    def directional_score(self, features: Dict, preferred_side: Optional[str] = None) -> Dict:
        mom = float(features.get("momentum", 0.0) or 0.0)
        vs_open = float(features.get("vs_open", 0.0) or 0.0)
        other_vs_open = float(features.get("other_vs_open", 0.0) or 0.0)
        window_lead = float(features.get("window_lead", 0.0) or 0.0)  # 5m mid bias for 15m
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
        fee_rate = float(features.get("fee_rate") or self.fee_rate)
        time_left = float(features.get("time_remaining_pct", 0.5) or 0.5)

        if depth < self.min_depth_usd:
            return self._reject("thin_book")
        if remaining_s > window_s + 30:
            return self._reject("too_early")
        if remaining_s < 30:
            return self._reject("near_expiry")
        if time_left < self.min_time_remaining:
            return self._reject("near_expiry")
        if entry < 0.025 or entry > 0.975:
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

        # Stronger TWAP-lock near end
        open_w = 0.25 if remaining_s > 220 else (0.70 if remaining_s < 100 else 0.50)
        directional_drive = (1 - open_w) * signed_mom + open_w * signed_open

        # Cross-asset lag
        lag_boost = 0.0
        if abs(signed_other) > 0.28 and abs(signed_open) < abs(signed_other) * 0.55:
            lag_boost = 0.10 * signed_other * (1.1 if self.degen else 0.7)

        # Cross-window lead-lag (5m informing 15m)
        lead_boost = 0.0
        if window_s >= 800 and abs(signed_lead) > 0.35:
            lead_boost = 0.12 * signed_lead * (1.15 if self.degen else 0.8)

        # Each feature set is built from the token being considered, so its
        # microprice sign is already in that token's own UP/DOWN coordinates.
        signed_micro = micro
        micro_boost = float(np.clip(signed_micro * 0.20, -0.06, 0.06))
        total_drive = directional_drive + lag_boost * 0.6 + lead_boost * 0.5 + micro_boost

        if total_drive < self.min_abs_momentum * 0.30:
            return self._reject("against_mom")

        if abs(total_drive) < self.min_abs_momentum * 0.7 and abs(signed_imb) < 0.08:
            return self._reject("weak_signal")

        time_term = 1.0 - abs(time_left - 0.28)
        cheap_term = max(0.0, (0.55 - entry) / 0.55)

        twap_lock = 0.0
        if remaining_s <= 120 and total_drive > 0.20 and entry < 0.60:
            twap_lock = 0.18 * total_drive * (1.4 if remaining_s < 80 else 1.0)

        raw = (
            0.38 * signed_mom
            + 0.35 * signed_open
            + 0.08 * signed_imb
            + 0.04 * float(np.clip(signed_micro, -1.0, 1.0))
            + 0.05 * time_term
            + 0.08 * cheap_term
            + lag_boost
            + lead_boost
            + twap_lock
        )

        if self.degen:
            raw += 0.035 * max(0.0, total_drive)

        if vol == "high":
            raw *= 0.84
        elif vol == "low":
            raw *= 1.07

        if spread > 0.065:
            raw *= 0.72
        elif spread > 0.038:
            raw *= 0.84

        score = float(np.clip(0.46 + raw * 0.62, 0.0, 1.0))

        drive_s = max(0.0, total_drive)
        imb_s = max(0.0, signed_imb)
        agree = 1.0 if (total_drive > 0.03 and signed_imb > -0.08) else 0.42
        signal_strength = float(np.clip(0.72 * drive_s + 0.28 * imb_s, 0.0, 1.0)) * agree
        cheap_boost = 1.0 + 0.28 * cheap_term
        max_lift = self.max_prob_lift * (1.18 if self.degen else 1.0)
        lift = min(signal_strength * max_lift * cheap_boost, max_lift)
        est_prob = float(np.clip(entry + lift, 0.025, 0.975))

        # This is a heuristic ranking proxy, not an independently estimated
        # probability. It must never be presented as calibrated EV.
        heuristic_edge = est_prob - entry - self._effective_fee(entry, fee_rate)
        min_edge = self.min_edge if not self.degen else max(0.008, self.min_edge * 0.60)
        if heuristic_edge < min_edge:
            return self._reject("heuristic_edge_below_costs")

        signal_pass = (
            score >= self.min_score
            and heuristic_edge >= min_edge
            and total_drive >= self.min_abs_momentum * 0.65
            and entry <= self.max_entry
        )
        if not signal_pass:
            return self._reject("signal_threshold")

        if not self.allow_heuristic_paper_entries:
            self.reject_counts["uncalibrated_probability"] = (
                self.reject_counts.get("uncalibrated_probability", 0) + 1
            )
            return {
                "type": "directional",
                "score": score,
                "direction": direction,
                "est_prob": None,
                "edge_after_fees": None,
                "heuristic_probability_proxy": est_prob,
                "heuristic_edge_proxy": heuristic_edge,
                "probability_kind": "uncalibrated_heuristic_proxy",
                "entry": entry,
                "pass": False,
                "reason": "uncalibrated_probability",
                "components": {
                    "drive": total_drive,
                    "imbalance": signed_imb,
                    "lag_boost": lag_boost,
                    "lead_boost": lead_boost,
                    "microprice_boost": micro_boost,
                    "twap_lock": twap_lock,
                    "heuristic_edge_proxy": heuristic_edge,
                    "effective_fee": self._effective_fee(entry, fee_rate),
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
            "probability_kind": "uncalibrated_heuristic_proxy",
            "entry": entry,
            "pass": True,
            "paper_probe": True,
            "components": {
                "drive": total_drive,
                "imbalance": signed_imb,
                "lag_boost": lag_boost,
                "lead_boost": lead_boost,
                "microprice_boost": micro_boost,
                "twap_lock": twap_lock,
                "heuristic_edge_proxy": heuristic_edge,
                "effective_fee": self._effective_fee(entry, fee_rate),
            },
        }

    def evaluate(self, features, up_mid=None, down_mid=None, up_ask=None, down_ask=None):
        arb = self.complete_set_arb(up_mid, down_mid, up_ask, down_ask)
        if arb:
            return arb
        return self.directional_score(features)
