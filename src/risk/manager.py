"""
Risk Manager - aggressive but controlled.
Kelly-lite sizing, exploration budget, hard circuit breakers.
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
        bank = max(1.0, _finite(starting_bankroll, 1000.0))
        self.bankroll = bank
        self.peak_bankroll = bank
        self.daily_pnl = 0.0
        self.current_day = _utc_today()
        self.paused = False
        self.pause_reason = None
        self._pause_logged = False
        self.exploration_used_today = 0.0

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
        # Never allow peak < bank (corrupt state)
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

        max_dd = _finite(self.config.get("max_drawdown_pct", 0.15), 0.15)
        daily_lim = _finite(self.config.get("daily_loss_limit_pct", 0.08), 0.08)
        max_dd = max(0.01, min(0.95, max_dd))
        daily_lim = max(0.01, min(0.95, daily_lim))

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
        if open_count >= int(self.config.get("max_concurrent", 4) or 4):
            return False
        size_usd = _finite(size_usd, 0.0)
        if size_usd <= 0:
            return False
        if self.bankroll <= 0:
            return False
        max_pct = _finite(self.config.get("max_position_pct", 0.05), 0.05)
        max_pct = max(0.001, min(0.25, max_pct))
        max_size = self.bankroll * max_pct
        return size_usd <= max_size + 1e-6

    def size_position(
        self,
        confidence: float,
        est_prob: Optional[float] = None,
        price: float = 0.5,
        is_exploration: bool = False,
        edge_after_fees: Optional[float] = None,
    ) -> float:
        max_pct = _finite(self.config.get("max_position_pct", 0.05), 0.05)
        max_pct = max(0.001, min(0.25, max_pct))
        base = self.bankroll * max_pct
        confidence = _finite(confidence, 0.5)
        price = _finite(price, 0.5)

        if is_exploration:
            explor_pct = _finite(self.learning_cfg.get("exploration_pct", 0.05), 0.05)
            remaining = max(0.0, self.bankroll * explor_pct - self.exploration_used_today)
            return min(base * 0.35, remaining)

        if edge_after_fees is not None:
            eaf = _finite(edge_after_fees, -1.0)
            if eaf <= 0:
                return 0.0
        if est_prob is not None and 0.01 < price < 0.99:
            ep = _finite(est_prob, 0.5)
            edge = ep - price
            if edge_after_fees is not None:
                edge = _finite(edge_after_fees, edge)
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
        self.exploration_used_today += max(0.0, _finite(size, 0.0))

    def record_pnl(self, pnl: float):
        p = _finite(pnl, 0.0)
        self.daily_pnl += p
        self.update_bankroll(self.bankroll + p)
