"""
Risk Manager - aggressive but controlled.
Includes Kelly-lite sizing, exploration budget, and hard circuit breakers.
"""
from dataclasses import dataclass, field
from typing import List, Dict, Optional
import logging
from datetime import datetime, date

logger = logging.getLogger("astf.risk")

@dataclass
class Position:
    market_id: str
    direction: str
    size_usd: float
    entry_price: float
    token_id: str
    opened_at: datetime = field(default_factory=datetime.utcnow)

class RiskManager:
    def __init__(self, config: dict, starting_bankroll: float):
        self.config = config.get("risk", {})
        self.learning_cfg = config.get("learning", {})
        self.bankroll = starting_bankroll
        self.peak_bankroll = starting_bankroll
        self.positions: List[Position] = []
        self.daily_pnl = 0.0
        self.current_day = date.today()
        self.paused = False
        self.pause_reason = None
        self.exploration_used_today = 0.0

    def update_bankroll(self, new_value: float):
        self.bankroll = new_value
        if new_value > self.peak_bankroll:
            self.peak_bankroll = new_value

        today = date.today()
        if today != self.current_day:
            self.daily_pnl = 0.0
            self.exploration_used_today = 0.0
            self.current_day = today

        self._check_circuit_breakers()

    def _check_circuit_breakers(self):
        dd = (self.peak_bankroll - self.bankroll) / self.peak_bankroll if self.peak_bankroll > 0 else 0
        daily_loss_pct = -self.daily_pnl / self.peak_bankroll if self.peak_bankroll > 0 else 0

        if dd >= self.config.get("max_drawdown_pct", 0.15):
            self.paused = True
            self.pause_reason = f"Max drawdown hit: {dd:.1%}"
            logger.warning(self.pause_reason)
        elif daily_loss_pct >= self.config.get("daily_loss_limit_pct", 0.08):
            self.paused = True
            self.pause_reason = f"Daily loss limit hit: {daily_loss_pct:.1%}"
            logger.warning(self.pause_reason)

    def can_open(self, size_usd: float) -> bool:
        if self.paused:
            return False
        if len(self.positions) >= self.config.get("max_concurrent", 4):
            return False
        max_size = self.bankroll * self.config.get("max_position_pct", 0.05)
        return size_usd <= max_size + 1e-6

    def size_position(self, confidence: float, est_prob: Optional[float] = None, price: float = 0.5, is_exploration: bool = False) -> float:
        """
        Kelly-lite sizing when we have an estimated probability.
        Falls back to confidence scaling otherwise.
        Exploration positions are deliberately smaller.
        """
        max_pct = self.config.get("max_position_pct", 0.05)
        base = self.bankroll * max_pct

        if is_exploration:
            explor_pct = self.learning_cfg.get("exploration_pct", 0.05)
            remaining = max(0.0, self.bankroll * explor_pct - self.exploration_used_today)
            return min(base * 0.4, remaining)

        if est_prob is not None and 0.01 < price < 0.99:
            # Approximate edge
            edge = est_prob - price
            if edge <= 0:
                return base * 0.25
            # Fractional Kelly (very conservative)
            kelly = edge / (1.0 - price) if price < 1 else 0
            kelly = max(0.0, min(kelly * 0.25, max_pct))  # 1/4 Kelly, capped
            size = self.bankroll * kelly
            return max(base * 0.25, min(base, size))

        # Confidence scaling fallback
        scale = max(0.25, min(1.0, (confidence - 0.5) / 0.4))
        return base * scale

    def record_exploration(self, size: float):
        self.exploration_used_today += size

    def record_pnl(self, pnl: float):
        self.daily_pnl += pnl
        self.update_bankroll(self.bankroll + pnl)
