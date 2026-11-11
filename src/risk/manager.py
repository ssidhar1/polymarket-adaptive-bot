"""
Risk Manager - position sizing + hard circuit breakers.
"""
from dataclasses import dataclass, field
from typing import List, Dict
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
        self.bankroll = starting_bankroll
        self.peak_bankroll = starting_bankroll
        self.positions: List[Position] = []
        self.daily_pnl = 0.0
        self.current_day = date.today()
        self.paused = False
        self.pause_reason = None

    def update_bankroll(self, new_value: float):
        self.bankroll = new_value
        if new_value > self.peak_bankroll:
            self.peak_bankroll = new_value

        # Daily reset
        today = date.today()
        if today != self.current_day:
            self.daily_pnl = 0.0
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

    def size_position(self, confidence: float) -> float:
        base = self.bankroll * self.config.get("max_position_pct", 0.05)
        # Scale by confidence (0.7 → full, lower → smaller)
        scale = max(0.3, min(1.0, (confidence - 0.5) / 0.4))
        return base * scale

    def record_pnl(self, pnl: float):
        self.daily_pnl += pnl
        self.update_bankroll(self.bankroll + pnl)
