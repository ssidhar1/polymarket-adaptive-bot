"""
Risk Manager - aggressive but controlled.
Kelly-lite sizing, exploration budget, hard circuit breakers.
"""
from typing import Optional
import logging
from datetime import date

logger = logging.getLogger("astf.risk")


class RiskManager:
    def __init__(self, config: dict, starting_bankroll: float):
        self.config = config.get("risk", {})
        self.learning_cfg = config.get("learning", {})
        self.bankroll = float(starting_bankroll)
        self.peak_bankroll = float(starting_bankroll)
        self.daily_pnl = 0.0
        self.current_day = date.today()
        self.paused = False
        self.pause_reason = None
        self.exploration_used_today = 0.0

    def update_bankroll(self, new_value: float):
        self.bankroll = float(new_value)
        if self.bankroll > self.peak_bankroll:
            self.peak_bankroll = self.bankroll

        today = date.today()
        if today != self.current_day:
            self.daily_pnl = 0.0
            self.exploration_used_today = 0.0
            self.current_day = today
            if self.paused and self.pause_reason and "Daily loss" in self.pause_reason:
                self.paused = False
                self.pause_reason = None
                logger.info("Daily loss pause cleared (new day)")

        self._check_circuit_breakers()

    def _check_circuit_breakers(self):
        if self.peak_bankroll <= 0:
            return
        dd = (self.peak_bankroll - self.bankroll) / self.peak_bankroll
        daily_loss_pct = -self.daily_pnl / self.peak_bankroll if self.daily_pnl < 0 else 0.0

        if dd >= self.config.get("max_drawdown_pct", 0.15):
            self.paused = True
            self.pause_reason = f"Max drawdown hit: {dd:.1%}"
            logger.warning(self.pause_reason)
        elif daily_loss_pct >= self.config.get("daily_loss_limit_pct", 0.08):
            self.paused = True
            self.pause_reason = f"Daily loss limit hit: {daily_loss_pct:.1%}"
            logger.warning(self.pause_reason)

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

        # Never force size on negative / missing edge
        if edge_after_fees is not None and edge_after_fees <= 0:
            return 0.0
        if est_prob is not None and 0.01 < price < 0.99:
            edge = est_prob - price
            if edge_after_fees is not None:
                edge = edge_after_fees
            if edge <= 0:
                return 0.0
            # quarter-Kelly on binary: f* = edge / (1 - price) roughly for payoff 1
            kelly = edge / max(1e-6, (1.0 - price))
            kelly = max(0.0, min(kelly * 0.2, max_pct))
            size = self.bankroll * kelly
            # scale up a bit with confidence, but never below 0 when edge is weak
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
