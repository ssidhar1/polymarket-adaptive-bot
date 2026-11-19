"""Paper-only strategy sleeves with settlement-only compounding.

No mark-to-market gain can increase a sleeve. Only realized, settled net P&L
changes strategy equity. Reservations reduce deployable cash until released.
"""
from __future__ import annotations

from decimal import Decimal


D = Decimal


class AllocationError(ValueError):
    pass


class StrategyCapitalAllocator:
    def __init__(
        self,
        starting_bankroll: Decimal | str | float,
        weights: dict[str, Decimal | str | float],
        *,
        reserve_cash_pct: Decimal | str | float = D("0.20"),
        max_strategy_pct: Decimal | str | float = D("0.25"),
        max_position_pct: Decimal | str | float = D("0.04"),
    ) -> None:
        self.starting_bankroll = D(str(starting_bankroll))
        self.weights = {k: D(str(v)) for k, v in weights.items()}
        self.reserve_cash_pct = D(str(reserve_cash_pct))
        self.max_strategy_pct = D(str(max_strategy_pct))
        self.max_position_pct = D(str(max_position_pct))
        self.realized_net_pnl = {k: D("0") for k in self.weights}
        self.reserved = {k: D("0") for k in self.weights}
        self._validate()

    def _validate(self) -> None:
        if self.starting_bankroll <= 0 or not self.weights:
            raise AllocationError("positive bankroll and at least one strategy are required")
        if any(not k or v < 0 for k, v in self.weights.items()):
            raise AllocationError("strategy names must be set and weights non-negative")
        if sum(self.weights.values()) <= 0:
            raise AllocationError("at least one strategy weight must be positive")
        if not D("0") <= self.reserve_cash_pct < D("1"):
            raise AllocationError("reserve_cash_pct must be in [0, 1)")
        if not D("0") < self.max_strategy_pct <= D("1"):
            raise AllocationError("max_strategy_pct must be in (0, 1]")
        if not D("0") < self.max_position_pct <= self.max_strategy_pct:
            raise AllocationError("max_position_pct must be positive and no larger than strategy cap")

    @property
    def total_equity(self) -> Decimal:
        return self.starting_bankroll + sum(self.realized_net_pnl.values())

    @property
    def cash_reserve(self) -> Decimal:
        return max(D("0"), self.total_equity * self.reserve_cash_pct)

    def sleeve_equity(self, strategy: str) -> Decimal:
        self._require_strategy(strategy)
        total_weight = sum(self.weights.values())
        seed = self.starting_bankroll * (D("1") - self.reserve_cash_pct)
        seed *= self.weights[strategy] / total_weight
        return max(D("0"), seed + self.realized_net_pnl[strategy])

    def available(self, strategy: str) -> Decimal:
        return max(D("0"), self.sleeve_equity(strategy) - self.reserved[strategy])

    def allocation(self, strategy: str) -> dict[str, Decimal]:
        equity = max(D("0"), self.total_equity)
        cap = equity * self.max_strategy_pct
        sleeve = min(self.sleeve_equity(strategy), cap)
        return {
            "sleeve_equity": sleeve,
            "reserved": self.reserved[strategy],
            "available": min(self.available(strategy), sleeve),
            "max_position": min(sleeve * self.max_position_pct, self.available(strategy)),
        }

    def reserve(self, strategy: str, amount: Decimal | str | float) -> Decimal:
        self._require_strategy(strategy)
        value = D(str(amount))
        if value <= 0:
            raise AllocationError("reservation must be positive")
        limits = self.allocation(strategy)
        if value > limits["available"] or value > limits["max_position"]:
            raise AllocationError("reservation exceeds available strategy or per-position capital")
        self.reserved[strategy] += value
        return self.reserved[strategy]

    def release(self, strategy: str, amount: Decimal | str | float) -> Decimal:
        self._require_strategy(strategy)
        value = D(str(amount))
        if value <= 0 or value > self.reserved[strategy]:
            raise AllocationError("invalid reservation release")
        self.reserved[strategy] -= value
        return self.reserved[strategy]

    def record_settlement(self, strategy: str, realized_net_pnl: Decimal | str | float) -> Decimal:
        """Apply settled net P&L only; callers must not pass open/unrealized P&L."""
        self._require_strategy(strategy)
        pnl = D(str(realized_net_pnl))
        self.realized_net_pnl[strategy] += pnl
        if self.total_equity <= 0:
            raise AllocationError("settlement would exhaust portfolio equity")
        return self.sleeve_equity(strategy)

    def snapshot(self) -> dict[str, object]:
        return {
            "paper_only": True,
            "total_equity": self.total_equity,
            "cash_reserve": self.cash_reserve,
            "strategies": {name: self.allocation(name) for name in sorted(self.weights)},
            "compounding_basis": "settled realized net P&L only",
        }

    def _require_strategy(self, strategy: str) -> None:
        if strategy not in self.weights:
            raise AllocationError(f"unknown strategy: {strategy}")
