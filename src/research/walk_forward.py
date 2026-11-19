"""Chronological evaluator for precomputed, event-time probability forecasts.

This is an evaluation layer, not a training algorithm. Forecasts must be
persisted when generated; labels are joined only after authoritative resolution.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Iterable


@dataclass(frozen=True)
class ForecastOutcome:
    strategy: str
    market_id: str
    observed_at: datetime
    resolution_at: datetime
    probability_yes: float
    entry_price: float
    outcome_yes: bool
    fees_per_share: float
    slippage_per_share: float


@dataclass(frozen=True)
class WalkForwardFold:
    train_count: int
    test_count: int
    test_start: datetime
    test_end: datetime
    brier_score: float
    mean_net_pnl_per_share: float
    total_net_pnl_per_share: float
    wins: int


def validate_forecast(row: ForecastOutcome) -> None:
    if not row.strategy or not row.market_id:
        raise ValueError("strategy and market_id are required")
    if row.observed_at.tzinfo is None or row.resolution_at.tzinfo is None:
        raise ValueError("forecast and resolution timestamps must be timezone-aware")
    if row.observed_at >= row.resolution_at:
        raise ValueError("forecast must be recorded before market resolution")
    numeric = (row.probability_yes, row.entry_price, row.fees_per_share, row.slippage_per_share)
    if any(not isfinite(float(v)) for v in numeric):
        raise ValueError("forecast inputs must be finite")
    if not 0 <= row.probability_yes <= 1 or not 0 < row.entry_price < 1:
        raise ValueError("probability and entry price are outside valid bounds")
    if row.fees_per_share < 0 or row.slippage_per_share < 0:
        raise ValueError("cost estimates cannot be negative")


def walk_forward_evaluate(
    records: Iterable[ForecastOutcome],
    *,
    min_train: int = 30,
    test_size: int = 20,
) -> list[WalkForwardFold]:
    """Score successive chronological test windows after a minimum history."""
    rows = list(records)
    for row in rows:
        validate_forecast(row)
    rows.sort(key=lambda row: row.observed_at)
    if min_train < 1 or test_size < 1:
        raise ValueError("min_train and test_size must be positive")
    if len({(r.strategy, r.market_id) for r in rows}) != len(rows):
        raise ValueError("duplicate strategy/market forecast records are not allowed")

    folds: list[WalkForwardFold] = []
    start = min_train
    while start < len(rows):
        end = min(len(rows), start + test_size)
        train = rows[:start]
        test = rows[start:end]
        if not test:
            break
        # The train slice is deliberately not used to refit a model here;
        # model-specific fitting/calibration must be performed by the caller.
        brier = sum((r.probability_yes - float(r.outcome_yes)) ** 2 for r in test) / len(test)
        pnls = [
            (1.0 if r.outcome_yes else 0.0) - r.entry_price
            - r.fees_per_share - r.slippage_per_share
            for r in test
        ]
        folds.append(WalkForwardFold(
            train_count=len(train),
            test_count=len(test),
            test_start=test[0].observed_at,
            test_end=test[-1].observed_at,
            brier_score=brier,
            mean_net_pnl_per_share=sum(pnls) / len(pnls),
            total_net_pnl_per_share=sum(pnls),
            wins=sum(1 for pnl in pnls if pnl > 0),
        ))
        start = end
    return folds
