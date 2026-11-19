"""Independent strategy modules for the unified paper-trading engine."""

from .base import Opportunity, Strategy, StrategyRegistry
from .capital_allocator import StrategyCapitalAllocator

__all__ = ["Opportunity", "Strategy", "StrategyRegistry", "StrategyCapitalAllocator"]
