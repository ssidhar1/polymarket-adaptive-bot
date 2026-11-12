"""
Risk Manager - aggressive but controlled.
Kelly-lite sizing, exploration budget, hard circuit breakers.
"""
from typing import Optional
import logging
from datetime import datetime, timezone

logger = logging.getLogger("astf.risk")


def _utc_today():
    return datetime.now(timezone.utc).date()


class RiskManager:
    def __init__(self, config: dict, starting_bankroll: float):
        self.config = config.get("risk", {})
        self.learning_cfg = config.get("learning", {})
        self.bankroll = float(starting_bankroll)
        self.peak_bankroll = float(starting_bankroll)
        self.daily_pnl = 0.0
        self.current_day = _utc_today()
        self.paused = False
        self.pause_reason = None
        self._pause_logged = False
        self.exploration_used_today = 0.0

    def clear_daily_pause(self, reason: str = "manual/restart"):
        """Clear daily-loss pause only (drawdown pause stays)."""
        if self.paused and self.pause_reason and "Daily loss" in (self.pause_reason or ""):
            self.paused = False
            self.pause_reason = None
            self.daily_pnl = 0.0
            self._pause_logged = False
            logger.info(f"Daily loss pause cleared ({reason})")

    def update_bankroll(self, new_value: float):
        self.bankroll = float(new_value)
        if self.bankroll > self.peak_bankroll:
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

        max_dd = self.config.get("max_drawdown_pct", 0.15)
        daily_lim = self.config.get("daily_loss_limit_pct", 0.08)

        if dd >= max_dd:
            self.paused = True
            self.pause_reason = f"Max drawdown hit: {dd:.1%}"
            if not self._pause_logged:
                logger.warning(
                    f"{self.pause_reason} | bank=${self.bankroll:.2f} peak=${self.peak_bankroll:.2f}"
                )
                self._pause_logged = True
        elif daily_loss_pct >= daily_lim:
            self.paused = True
            self.pause_reason = f"Daily loss limit hit: {daily_loss_pct:.1%}"
            if not self._pause_logged:
                logger.warning(
                    f"{self.pause_reason} | daily_pnl=${self.daily_pnl:.2f} "
                    f"bank=${self.bankroll:.2f} peak=${self.peak_bankroll:.2f} "
                    f"(clears next UTC day or on service restart)"
                )
                self._pause_logged = True

    def can_open(self, size_usd: float, open_count: int = 0) -> bool:
        if self.paused:
            return False
        if open_count >= self.config.get("max_concurrent", 4):
            return False
        if size_usd <= 0:
            return False
        max_size = self.bankroll * self.config.get("max_position_pct", 0.05)
        return size_usd <= max_size + 1e-6

    def size_position(
        self,
        confidence: float,
        est_prob: Optional[float] = None,
        price: float = 0.5,
        is_exploration: bool = False,
        edge_after_fees: Optional[float] = None,
    ) -> float:
        max_pct = self.config.get("max_position_pct", 0.05)
        base = self.bankroll * max_pct

        if is_exploration:
            explor_pct = self.learning_cfg.get("exploration_pct", 0.05)
            remaining = max(0.0, self.bankroll * explor_pct - self.exploration_used_today)
            return min(base * 0.35, remaining)

        if edge_after_fees is not None and edge_after_fees <= 0:
            return 0.0
        if est_prob is not None and 0.01 < price < 0.99:
            edge = est_prob - price
            if edge_after_fees is not None:
                edge = edge_after_fees
            if edge <= 0:
                return 0.0
            kelly = edge / max(1e-6, (1.0 - price))
            kelly = max(0.0, min(kelly * 0.2, max_pct))
            size = self.bankroll * kelly
            conf_scale = max(0.5, min(1.0, (confidence - 0.5) / 0.35))
            size *= conf_scale
            return max(0.0, min(base, size))

        scale = max(0.0, min(1.0, (confidence - 0.55) / 0.35))
        return base * scale if scale > 0 else 0.0

    def record_exploration(self, size: float):
        self.exploration_used_today += size

    def record_pnl(self, pnl: float):
        self.daily_pnl += pnl
        self.update_bankroll(self.bankroll + pnl)
