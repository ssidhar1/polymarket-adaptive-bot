# Unified Polymarket Engine — Master Plan

## Mission
Build one research and paper-execution engine that can discover, compare, and allocate capital across independent Polymarket strategies. The objective is to establish a repeatable, positive **net** edge with measured drawdown and execution realism. Profit is not guaranteed; no strategy receives more capital because of attractive backtests or unverified win rates.

## Repository decision
`polymarket-adaptive-bot` is the canonical runtime and deployment repository. Penny Hunter remains a source of strategy research and scanner ideas, not a second production process. Port ideas selectively after code review. Do not import Penny Hunter's placeholder fair values, zero-fee assumptions, incomplete execution, or `pytest || true` workflow.

## Shared architecture
1. **Market data plane:** Gamma discovery, CLOB books/trades, market metadata and resolution rules, crypto spot feeds, weather station observations, and supported public information sources. Normalize timestamps, token IDs, market IDs, outcome mapping, and source provenance.
2. **Opportunity contract:** strategies emit candidates only. Every candidate includes strategy, market/token, side, observation time, executable entry price, fair probability (or explicitly unknown), fees, slippage, depth, resolution time, evidence and risk flags. Unknown fair value means no positive edge may be asserted.
3. **Independent strategy modules:**
   - ASTF intraday BTC/ETH 5m and 15m momentum, cross-asset lag, cross-window lead-lag, microstructure and regime-conditioned setups.
   - Weekly price-range / threshold markets: distribution-based probability estimates using external spot/volatility data, time remaining, exact strike/bucket boundaries and market rules.
   - Near-resolution: only after independently verified outcome evidence and exact resolution rules; time-to-expiry alone is not an edge.
   - Weather station: airport ICAO mapping, authoritative METAR history/forecast, unit/bucket parsing, observation age and settlement source verification.
   - Cross-market / complete-set arbitrage: observe first; execution stays disabled until both legs can be modeled with partial fills, hedging and atomic-risk controls.
   - Information latency / settlement intelligence: source timestamps, provenance, independent corroboration and stale-source rejection.
   - Liquidity provision: research-only until queue position, adverse selection, maker/taker fees and cancellation behavior are simulated.
4. **Evidence and probability layer:** compare independent model probability to executable price. Calibrate probabilities out of sample by strategy, market family and horizon. Reject missing, stale, conflicting or rule-ambiguous evidence. Never substitute a hardcoded probability for a model.
5. **Shared portfolio/risk:** one global bankroll; per-strategy sleeves; correlated-market exposure caps; per-market and per-event limits; daily loss and drawdown stops; cash reserve; no leverage; kill switch. Strategy isolation prevents one scanner from consuming another sleeve.
6. **Paper execution:** one durable event ledger for entries, partial fills, exits, fees, settlement and restart replay. Use executable order-book depth, conservative queue/fill assumptions, spread crossing, latency and market-specific fee schedules. Label modeled fees separately from exchange-reported fees.
7. **Research and validation:** event-time historical replay, no look-ahead, no survivorship bias, walk-forward training/testing, purged/embargoed splits where labels overlap, baseline comparisons, parameter sensitivity, realistic costs and out-of-sample reporting.
8. **Operations:** persistent Northflank volume for SQLite/state, bounded retries, health/readiness, stale-feed fail-closed behavior, graceful shutdown, CI that fails on test failure, and reproducible run manifests.

## Capital allocation and compounding
- Starting virtual bankroll remains $1,000 unless an explicit reset is requested and no open positions exist.
- A configurable percentage remains unallocated as cash reserve.
- Each strategy receives an independent sleeve and maximum allocation.
- Open/unrealized P&L never increases deployable capital.
- Only realized, settled **net** P&L changes a strategy sleeve's equity.
- Reserve exposure is deducted before new orders; releases happen only after durable exit/settlement events.
- Reduce or pause a strategy after drawdown, poor calibration, fee-model uncertainty, or feed-quality failure.
- Never annualize a handful of high-probability wins into a promised yield. A 99-cent share still has a one-dollar loss tail if it resolves to zero.

## PolyBonds / weekly-market research
The requested reference is `https://www.polybonds.fun/?range=week`. Automated retrieval from this environment did not return the page, so its exact ranking formula, filters, data source and claims remain unverified. Do not treat the site as an execution venue or trusted probability source. Recreate the useful idea independently from Polymarket market data: find markets resolving within the next seven days, inspect exact rules, rank by net expected value, executable depth, spread, time-to-resolution, source quality and calibrated probability. Preserve the original source URL in research records when later inspected manually.

## Promotion gates
A strategy progresses through: (A) scanner-only observations; (B) deterministic unit/replay tests; (C) historical walk-forward; (D) paper shadow mode; (E) paper fills and authoritative settlements; (F) controlled capital allocation. No automatic live promotion.

Minimum evidence before increasing paper allocation: at least 100 fully resolved paper positions per strategy where feasible, positive net expectancy after conservative costs, acceptable drawdown, calibration analysis, stable performance across multiple walk-forward windows, and no unresolved accounting/data-quality defects. Smaller samples remain exploratory and are reported with uncertainty. These are research gates, not guarantees.

## Scorecard
Report per strategy and combined portfolio: candidate count, accepted/rejected reasons, entries, partial fills, completed resolutions, win rate, average win/loss, gross and net realized P&L, modeled fees, slippage, return on deployed capital, turnover, exposure, maximum drawdown, Brier score/calibration, sample size, and data-quality coverage. Separate open-position mark-to-market from realized results.

## Immediate implementation sequence
1. Shared opportunity/strategy contract and strategy-isolated capital allocator (introduced in this commit; not yet connected to live scanner loop).
2. Port Penny Hunter discovery adapters behind the shared contract, fixing token/outcome mapping and pagination.
3. Implement weekly price-range strategy with an explicit volatility/distribution model and historical replay; no fixed fair-value constants.
4. Add strategy-specific event-time datasets and walk-forward harness.
5. Connect strategy candidates to shared risk and existing durable paper lifecycle.
6. Add per-strategy scorecards and paper capital compounding from settlement events only.
7. Verify Northflank persistent storage, feed freshness, restarts and operational health.

## Safety boundary
Paper mode only. The dormant live adapter remains disconnected and `run_live()` must continue refusing execution. No private keys, wallet approvals, live orders, or real-money transactions are part of this plan.
