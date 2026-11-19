from decimal import Decimal

import pytest

from src.strategies.base import Opportunity, StrategyRegistry
from src.strategies.capital_allocator import AllocationError, StrategyCapitalAllocator


def test_opportunity_refuses_missing_fair_value_as_positive_edge():
    from datetime import datetime, timezone

    item = Opportunity(
        strategy="weekly_bonds", market_id="m1", token_id="t1", side="yes",
        observed_at=datetime.now(timezone.utc), executable_price=Decimal("0.95"),
        fair_probability=None, estimated_fees=Decimal("0"),
        estimated_slippage=Decimal("0"), available_depth=Decimal("100"),
    )
    assert item.expected_net_edge_per_share is None
    assert not item.is_evidence_complete


def test_opportunity_calculates_net_edge_after_costs():
    from datetime import datetime, timezone

    item = Opportunity(
        strategy="weather", market_id="m1", token_id="t1", side="yes",
        observed_at=datetime.now(timezone.utc), executable_price=Decimal("0.80"),
        fair_probability=Decimal("0.90"), estimated_fees=Decimal("0.02"),
        estimated_slippage=Decimal("0.01"), available_depth=Decimal("50"),
        evidence=("official_station_observation",),
    )
    assert item.expected_net_edge_per_share == Decimal("0.07")
    assert item.is_evidence_complete


def test_settled_profit_compounds_but_open_mark_does_not():
    allocator = StrategyCapitalAllocator(
        "1000", {"weekly": "0.5", "intraday": "0.5"},
        reserve_cash_pct="0.20", max_strategy_pct="0.50", max_position_pct="0.10",
    )
    before = allocator.sleeve_equity("weekly")
    allocator.reserve("weekly", "20")
    assert allocator.sleeve_equity("weekly") == before
    allocator.release("weekly", "20")
    after = allocator.record_settlement("weekly", "10")
    assert after > before
    assert allocator.total_equity == Decimal("1010")
    allocated = sum(allocator.sleeve_equity(name) for name in allocator.weights)
    assert allocated + allocator.cash_reserve == allocator.total_equity
    assert allocator.cash_reserve >= allocator.total_equity * allocator.reserve_cash_pct


def test_allocator_caps_positions_and_rejects_overallocation():
    allocator = StrategyCapitalAllocator(
        "1000", {"weekly": "1"}, reserve_cash_pct="0.20",
        max_strategy_pct="0.50", max_position_pct="0.05",
    )
    assert allocator.allocation("weekly")["max_position"] == Decimal("40.00")
    with pytest.raises(AllocationError):
        allocator.reserve("weekly", "41")


def test_strategy_registry_rejects_duplicate_names_and_isolates_scan_errors():
    class Good:
        name = "good"

        async def scan(self):
            return []

    class Broken:
        name = "broken"

        async def scan(self):
            raise RuntimeError("scanner down")

    registry = StrategyRegistry()
    registry.register(Good())
    registry.register(Broken())
    with pytest.raises(ValueError):
        registry.register(Good())
    assert registry.names() == ("broken", "good")
