"""
Risk Manager — aggressive compounding when edge is real.
"""
from typing import Optional
import logging
import math
from datetime import datetime, timezone

logger = logging.getLogger("astf.risk")


def _utc_today():
    return datetime.now(timezone.utc).date()


def _finite(x, default: float = 0.0) -> float:
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except (TypeError, ValueError):
        return default


class RiskManager:
    def __init__(self, config: dict, starting_bankroll: float):
        self.config = config.get("risk", {}) or {}
        self.learning_cfg = config.get("learning", {}) or {}
        self.signals_cfg = config.get("signals", {}) or {}
        bank = max(1.0, _finite(starting_bankroll, 1000.0))
        self.bankroll = bank
        self.peak_bankroll = bank
        self.daily_pnl = 0.0
        self.current_day = _utc_today()
        self.paused = False
        self.pause_reason = None
        self._pause_logged = False
        self.exploration_used_today = 0.0
        self.degen = bool(self.signals_cfg.get("degen_mode", True))

    def clear_daily_pause(self, reason: str = "manual/restart"):
        if self.paused and self.pause_reason and "Daily loss" in (self.pause_reason or ""):
            self.paused = False
            self.pause_reason = None
            self.daily_pnl = 0.0
            self._pause_logged = False
            logger.info(f"Daily loss pause cleared ({reason})")

    def update_bankroll(self, new_value: float):
        self.bankroll = max(0.0, _finite(new_value, self.bankroll))
        if self.bankroll > self.peak_bankroll:
            self.peak_bankroll = self.bankroll
        if self.peak_bankroll < self.bankroll:
            self.peak_bankroll = self.bankroll
        today = _utc_today()
        if today != self.current_day:
            self.daily_pnl = 0.0
            self.exploration_used_today = 0.0
            self.current_day = today
            if self.paused and self.pause_reason and "Daily loss" in (self.pause_reason or ""):
                self.paused = False
                self.pause_reason = None
                self._pause_logged = False
                logger.info("Daily loss pause cleared (new UTC day)")
        self._check_circuit_breakers()

    def _check_circuit_breakers(self):
        if self.peak_bankroll <= 0:
            return
        dd = (self.peak_bankroll - self.bankroll) / self.peak_bankroll
        daily_loss_pct = -self.daily_pnl / self.peak_bankroll if self.daily_pnl < 0 else 0.0
        max_dd = max(0.01, min(0.90, _finite(self.config.get("max_drawdown_pct", 0.15), 0.15)))
        daily_lim = max(0.01, min(0.90, _finite(self.config.get("daily_loss_limit_pct", 0.08), 0.08)))
        if dd >= max_dd:
            self.paused = True
            self.pause_reason = f"Max drawdown hit: {dd:.1%}"
            if not self._pause_logged:
                logger.warning(f"{self.pause_reason} | bank=${self.bankroll:.2f}")
                self._pause_logged = True
        elif daily_loss_pct >= daily_lim:
            self.paused = True
            self.pause_reason = f"Daily loss limit hit: {daily_loss_pct:.1%}"
            if not self._pause_logged:
                logger.warning(f"{self.pause_reason} | daily_pnl=${self.daily_pnl:.2f}")
                self._pause_logged = True

    def can_open(self, size_usd: float, open_count: int = 0) -> bool:
        if self.paused:
            return False
        if open_count >= int(self.config.get("max_concurrent", 5) or 5):
            return False
        size_usd = _finite(size_usd, 0.0)
        if size_usd <= 0 or self.bankroll <= 0:
            return False
        max_pct = max(0.001, min(0.30, _finite(self.config.get("max_position_pct", 0.05), 0.05)))
        return size_usd <= self.bankroll * max_pct + 1e-6

    def size_position(
        self,
        confidence: float,
        est_prob: Optional[float] = None,
        price: float = 0.5,
        is_exploration: bool = False,
        edge_after_fees: Optional[float] = None,
        regime: str = "normal",
    ) -> float:
        max_pct = max(0.001, min(0.30, _finite(self.config.get("max_position_pct", 0.05), 0.05)))
        base = self.bankroll * max_pct
        confidence = _finite(confidence, 0.5)
        price = _finite(price, 0.5)

        if is_exploration:
            explor_pct = _finite(self.learning_cfg.get("exploration_pct", 0.025), 0.025)
            remaining = max(0.0, self.bankroll * explor_pct - self.exploration_used_today)
            return min(base * 0.3, remaining)

        if edge_after_fees is not None:
            eaf = _finite(edge_after_fees, -1.0)
            if eaf <= 0:
                return 0.0
        else:
            eaf = 0.0

        if est_prob is not None and 0.01 < price < 0.99:
            ep = _finite(est_prob, 0.5)
            edge = eaf if eaf > 0 else (ep - price)
            if edge <= 0:
                return 0.0
            # More aggressive Kelly fraction in degen + favorable
            frac = 0.28 if self.degen else 0.18
            if regime == "favorable":
                frac *= 1.35
            elif regime == "chaotic":
                frac *= 0.55
            kelly = edge / max(1e-6, (1.0 - price))
            kelly = max(0.0, min(kelly * frac, max_pct * 1.4))
            size = self.bankroll * kelly
            conf_scale = max(0.55, min(1.15, (confidence - 0.48) / 0.30))
            size *= conf_scale
            return max(0.0, min(base * 1.25, size))

        scale = max(0.0, min(1.0, (confidence - 0.52) / 0.32))
        return base * scale if scale > 0 else 0.0

    def record_exploration(self, size: float):
        self.exploration_used_today += max(0.0, _finite(size, 0.0))

    def record_pnl(self, pnl: float):
        p = _finite(pnl, 0.0)
        self.daily_pnl += p
        self.update_bankroll(self.bankroll + p)
