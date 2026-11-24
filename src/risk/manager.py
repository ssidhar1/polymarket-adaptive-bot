"""
Risk manager with hard portfolio exposure limits and conservative sizing.
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
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def _pct(config: dict, key: str, default: float, low: float = 0.001, high: float = 0.90) -> float:
    return max(low, min(high, _finite(config.get(key, default), default)))


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
        self.peak_bankroll = max(self.peak_bankroll, self.bankroll)
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
        max_dd = _pct(self.config, "max_drawdown_pct", 0.12, 0.01, 0.90)
        daily_lim = _pct(self.config, "daily_loss_limit_pct", 0.05, 0.01, 0.90)
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

    def can_open(
        self,
        size_usd: float,
        open_count: int = 0,
        reserved_exposure: float = 0.0,
        asset_exposure: float = 0.0,
        window_exposure: float = 0.0,
    ) -> bool:
        if self.paused:
            return False
        if open_count >= int(self.config.get("max_concurrent", 6) or 6):
            return False
        size = _finite(size_usd, 0.0)
        if size <= 0 or self.bankroll <= 0:
            return False
        max_position = _pct(self.config, "max_position_pct", 0.04, 0.001, 0.30)
        total_cap = _pct(self.config, "max_total_exposure_pct", 0.20, 0.01, 0.90)
        asset_cap = _pct(self.config, "max_asset_exposure_pct", 0.14, 0.01, 0.90)
        window_cap = _pct(self.config, "max_window_exposure_pct", 0.18, 0.01, 0.90)
        if size > self.bankroll * max_position + 1e-6:
            return False
        if _finite(reserved_exposure) + size > self.bankroll * total_cap + 1e-6:
            return False
        if _finite(asset_exposure) + size > self.bankroll * asset_cap + 1e-6:
            return False
        if _finite(window_exposure) + size > self.bankroll * window_cap + 1e-6:
            return False
        return True

    def size_position(
        self,
        confidence: float,
        est_prob: Optional[float] = None,
        price: float = 0.5,
        is_exploration: bool = False,
        edge_after_fees: Optional[float] = None,
        regime: str = "normal",
    ) -> float:
        max_pct = _pct(self.config, "max_position_pct", 0.04, 0.001, 0.30)
        base = self.bankroll * max_pct
        confidence = _finite(confidence, 0.5)
        price = _finite(price, 0.5)

        if is_exploration:
            explor_pct = _pct(self.learning_cfg, "exploration_pct", 0.02, 0.001, 0.10)
            remaining = max(0.0, self.bankroll * explor_pct - self.exploration_used_today)
            return min(base * 0.25, remaining)

        eaf = _finite(edge_after_fees, -1.0) if edge_after_fees is not None else -1.0
        if eaf <= 0:
            return 0.0

        # Kelly is disabled until a separately validated calibration model is
        # explicitly enabled. The current ensemble probability is heuristic.
        calibrated = bool(self.config.get("probability_model_calibrated", False))
        if calibrated and est_prob is not None and 0.01 < price < 0.99:
            ep = max(0.01, min(0.99, _finite(est_prob, 0.5)))
            edge = ep - price
            if edge <= 0:
                return 0.0
            kelly_fraction = max(0.0, min(edge / max(1e-6, 1.0 - price) * 0.10, max_pct))
            return min(base, self.bankroll * kelly_fraction)

        confidence_scale = max(0.0, min(1.0, (confidence - 0.52) / 0.32))
        edge_scale = max(0.0, min(1.0, eaf / 0.05))
        regime_scale = 0.65 if regime == "chaotic" else (1.0 if regime == "normal" else 1.05)
        # Scale with bankroll growth (parabolic but capped). Base $1000 = 1.0x, $10k ~ 1.4x, $50k ~ 1.8x
        start = float(self.config.get("starting_bankroll", 1000.0) or 1000.0)
        growth = max(1.0, self.bankroll / max(start, 1.0))
        balance_scale = min(2.0, growth ** 0.25)  # soft compound boost, never more than 2x the pct
        return base * confidence_scale * edge_scale * regime_scale * balance_scale

    def record_exploration(self, size: float):
        self.exploration_used_today += max(0.0, _finite(size, 0.0))

    def record_pnl(self, pnl: float):
        p = _finite(pnl, 0.0)
        self.daily_pnl += p
        self.update_bankroll(self.bankroll + p)
