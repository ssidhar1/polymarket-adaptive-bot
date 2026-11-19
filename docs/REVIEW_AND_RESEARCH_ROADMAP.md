# ASTF Review and Research Roadmap

Review date: 2026-11-18

## What changed in this branch

1. Paper execution now models aggressive orders as taker fills, applies adverse slippage, and never assumes a maker rebate without evidence of a resting maker fill.
2. Adaptive regime changes now require a minimum sample, use size-normalized returns, and use Wilson confidence bounds before granting setup-bucket boosts.
3. Adaptive bucket state is versioned. Legacy buckets stored as raw USD P&L are ignored rather than mixed with the new normalized-return metric.
4. Market discovery now uses per-thread HTTP sessions. The previous shared lock enclosed the network request itself, serializing calls despite the ThreadPoolExecutor.

## High-priority issues still requiring work

### 1. Daily risk state — hardened, still needs fault-injection testing
This branch persists same-day realized P&L, pause state/reason, and exploration usage, restores them on restart, and no longer clears a daily pause on service startup. The peak bankroll is also restored and drawdown is recalculated. Validate with forced process termination immediately after a loss-limit breach and confirm the pause survives restart.

### 2. Stale positions — no longer booked as realized P&L
This branch removes midpoint and arbitrary 0.50 stale exits. Stale positions remain open until authoritative market resolution. The trade-off is that a market that never resolves or cannot be found can occupy a position slot indefinitely; add a separate unresolved-position alert/reconciliation workflow rather than fabricating a close.

### 3. The probability model is heuristic, not calibrated
The ensemble sets estimated probability by adding a hand-tuned signal lift to the current ask. This is not a calibrated forecast. Do not use this probability for Kelly sizing until it is validated out-of-sample with reliability curves, Brier score, log loss, and calibration by asset, time-to-expiry, and regime.

### 4. Paper fills still lack depth-aware execution
The current executor applies a conservative slippage haircut but does not walk the actual order book, model partial fills, or model queue position. Add depth-weighted VWAP, minimum fill ratio, spread crossing, and latency snapshots before interpreting paper P&L as executable.

### 5. Complete-set arbitrage is not atomic
The strategy submits UP and DOWN legs sequentially. If one leg fails or only partially fills, the first leg can remain directional. Add pair-level execution state, leg reconciliation, and a one-leg failure/hedge policy. Until then, report paired-arbitrage P&L separately and do not call it risk-free.

### 6. Exposure limits need portfolio-level accounting
Per-order size and concurrent-count checks do not fully cap total reserved notional, correlated BTC/ETH exposure, or same-window concentration. Add reserved exposure, asset/window concentration caps, and worst-case loss across all open binary positions.

## Strategy research queue

These are hypotheses to test, not claims of proven edge.

1. **Microprice / order-book pressure:** calculate size-weighted microprice, multi-level imbalance, spread changes, and short-lived quote pressure. Require synchronized timestamps and reject stale books.
2. **Cross-asset residual lead/lag:** estimate rolling BTC-to-ETH beta and trade only the residual move that ETH has not priced in. Avoid using a fixed raw BTC/ETH return comparison.
3. **Cross-window information transfer:** measure whether the 5-minute market's probability change predicts the 15-minute market after controlling for the underlying spot move, spread, and time remaining.
4. **Expiry hazard model:** learn conditional outcome probabilities from the underlying return distribution and realized volatility, conditional on time remaining. This should replace arbitrary TWAP-lock boosts only after walk-forward validation.
5. **Dislocation scanner:** compare UP ask + DOWN ask, bid-side unwind value, depth-weighted VWAP, fees, and latency. Require both legs to clear minimum fill depth and preserve pair-level atomicity.
6. **Regime-aware execution:** choose taker only when the predicted edge exceeds spread + fee + slippage + latency buffer; otherwise wait or skip. A simulated maker fill must model queue position and non-fill probability.
7. **Online calibration:** use a small, interpretable calibration layer trained only on resolved historical observations. Keep a frozen holdout and prevent leakage across overlapping 5m/15m windows.
8. **Exploration budget:** reserve a small, explicit paper-only budget for testing new signal families; never let exploration trades update production sizing until separately evaluated.

## Minimum evidence before considering live capital

- At least 500 resolved paper trades, with separate results by asset, window, strategy, and regime.
- Walk-forward tests with a frozen out-of-sample period and no overlapping-window leakage.
- Net expectancy after taker fees, realistic slippage, partial fills, and stale-data/latency penalties.
- Positive lower confidence bound on expectancy, not just positive point-estimate P&L.
- Brier score and calibration plots for probability forecasts.
- Maximum drawdown, tail loss, exposure concentration, and restart-recovery tests.
- Persistent Northflank volume mounted at `/app/data`; verify data survives a redeploy and restart.

## Northflank free-tier operating guidance

Keep the worker lightweight: avoid heavyweight ML dependencies and high-frequency polling that provides no measurable edge. The per-thread HTTP change restores concurrency, but request budgets and API rate limits still need monitoring. Use a persistent volume at `/app/data`; without it, the SQLite trade history and JSON runtime state may disappear on redeploy. The container health check currently verifies importability, not whether the strategy loop is alive or market data is fresh.

## Important interpretation

No strategy can promise extreme profits. Short-dated binary markets are highly competitive, and a small modeling or execution error can erase a seemingly large edge. The goal of this roadmap is to make every claimed edge measurable, falsifiable, and net of real costs.
