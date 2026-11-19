"""Per-strategy scorecards over resolved, event-time forecasts."""
from __future__ import annotations

from math import isfinite
from typing import Iterable

from src.research.walk_forward import ForecastOutcome, validate_forecast


def build_strategy_scorecard(records: Iterable[ForecastOutcome]) -> dict:
    rows = list(records)
    for row in rows:
        validate_forecast(row)
    if not rows:
        return {"sample_size": 0, "status": "insufficient_data"}
    rows.sort(key=lambda row: row.observed_at)
    pnls = [
        (1.0 if row.outcome_yes else 0.0) - row.entry_price
        - row.fees_per_share - row.slippage_per_share
        for row in rows
    ]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    brier = sum((row.probability_yes - float(row.outcome_yes)) ** 2 for row in rows) / len(rows)
    equity = peak = max_drawdown = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return {
        "strategy": rows[0].strategy,
        "sample_size": len(rows),
        "win_rate": len(wins) / len(rows),
        "brier_score": brier,
        "mean_net_pnl_per_share": sum(pnls) / len(pnls),
        "total_net_pnl_per_share": sum(pnls),
        "modeled_fees_per_share_total": sum(row.fees_per_share for row in rows),
        "modeled_slippage_per_share_total": sum(row.slippage_per_share for row in rows),
        "max_drawdown_per_share_sequence": max_drawdown,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else (float("inf") if wins else 0.0),
        "status": "research_only_no_capital_promotion",
        "pnl_basis": "one-share outcome payoff less supplied modeled fees and slippage",
    }
