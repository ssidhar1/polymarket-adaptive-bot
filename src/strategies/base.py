"""Shared, deliberately small contract for independent Polymarket strategies.

Strategies discover and score opportunities; they do not size positions, mutate
bankrolls, or execute orders. Those decisions belong to shared portfolio services.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class Opportunity:
    strategy: str
    market_id: str
    token_id: str
    side: str
    observed_at: datetime
    executable_price: Decimal
    fair_probability: Decimal | None
    estimated_fees: Decimal
    estimated_slippage: Decimal
    available_depth: Decimal
    resolution_at: datetime | None = None
    evidence: tuple[str, ...] = ()
    risk_flags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.strategy or not self.market_id or not self.token_id:
            raise ValueError("strategy, market_id, and token_id are required")
        if self.side.lower() not in {"yes", "no", "up", "down"}:
            raise ValueError("unsupported outcome side")
        if not Decimal("0") < self.executable_price < Decimal("1"):
            raise ValueError("executable_price must be between zero and one")
        if self.fair_probability is not None and not Decimal("0") <= self.fair_probability <= Decimal("1"):
            raise ValueError("fair_probability must be between zero and one")
        if min(self.estimated_fees, self.estimated_slippage, self.available_depth) < 0:
            raise ValueError("fees, slippage, and depth cannot be negative")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        if self.resolution_at is not None and self.resolution_at.tzinfo is None:
            raise ValueError("resolution_at must be timezone-aware")

    @property
    def expected_net_edge_per_share(self) -> Decimal | None:
        """Return a conservative net edge only when a real fair value exists."""
        if self.fair_probability is None:
            return None
        return self.fair_probability - self.executable_price - self.estimated_fees - self.estimated_slippage

    @property
    def is_evidence_complete(self) -> bool:
        return bool(self.evidence) and not self.risk_flags and self.fair_probability is not None


@runtime_checkable
class Strategy(Protocol):
    name: str

    async def scan(self) -> list[Opportunity]:
        """Return candidates only; never execute or mutate portfolio state."""


class StrategyRegistry:
    """Explicit registry that prevents duplicate names and isolates failures."""

    def __init__(self) -> None:
        self._strategies: dict[str, Strategy] = {}

    def register(self, strategy: Strategy) -> None:
        name = str(getattr(strategy, "name", "")).strip()
        if not name:
            raise ValueError("strategy must expose a non-empty name")
        if name in self._strategies:
            raise ValueError(f"strategy already registered: {name}")
        if not isinstance(strategy, Strategy):
            raise TypeError("strategy must implement async scan()")
        self._strategies[name] = strategy

    def get(self, name: str) -> Strategy:
        return self._strategies[name]

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._strategies))

    async def scan_all(self) -> dict[str, list[Opportunity] | Exception]:
        """Run scanners independently; one broken strategy cannot hide others."""
        import asyncio

        names = self.names()
        results = await asyncio.gather(
            *(self._strategies[name].scan() for name in names), return_exceptions=True
        )
        return {name: result for name, result in zip(names, results)}
