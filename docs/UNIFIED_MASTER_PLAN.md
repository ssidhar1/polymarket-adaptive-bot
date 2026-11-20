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


## Weekly market discovery adapter (implemented, observation-only)
- `src/strategies/weekly_price_ranges.py` pages Gamma market discovery for the next seven days, locally rechecks dates/status, maps the YES token from the actual `outcomes` array, and snapshots the CLOB ask plus top-five ask depth.
- It applies a non-zero fee estimate (Gamma fee schedule when present; conservative configured fallback otherwise) and explicit slippage assumption. Missing fee metadata is flagged.
- It deliberately emits `fair_probability=None` with `fair_value_unmodeled` and `resolution_rules_unverified` flags. It is a discovery feed, not a trade signal. It cannot be promoted into the paper executor until the probability model, exact resolution source, and event-time historical replay are validated.
- The scanner is not yet registered in `src/main.py`; `strategy_portfolio.enabled` remains false.


## Probability model and walk-forward evaluator (research scaffolding)
- `src/strategies/weekly_probability.py` provides a lognormal bucket/threshold probability primitive. Spot, annualized volatility, horizon and optional drift are explicit inputs; the default zero-drift assumption is not calibrated and is not a trade signal.
- `src/research/walk_forward.py` validates event-time forecast records, rejects forecasts timestamped at/after resolution, and scores chronological out-of-sample test windows with Brier score and per-share net P&L after supplied fees/slippage.
- The evaluator does not fit or calibrate a model. A future strategy-specific trainer must use only data available before each test window, and forecasts must be persisted before outcomes are known. No historical dataset or performance claim is included in this commit.
- Remaining blockers before weekly paper execution: authoritative resolution-rule parser/audit, event-time spot and volatility history, volatility calibration by horizon/regime, market-specific fee validation, durable forecast capture, and integration with shared risk + paper ledger.


## Event-time forecast storage and strategy scorecards
- `src/research/forecast_store.py` stores immutable forecast observations separately from later resolution events. Unmodeled scanner observations may be retained with a null probability; they are excluded from probability scorecards. Resolution events require a later timestamp and a source reference.
- `src/research/scorecard.py` reports sample size, win rate, Brier score, per-share net P&L after supplied fees/slippage, cost totals and drawdown sequence. It is explicitly research-only and does not allocate capital.
- These research components are not yet connected to the production scan loop or existing paper lifecycle. Until that integration is reviewed, no weekly candidates can open positions and no allocator compounding is claimed.


## Critical realism fixes — November 2026
- Short-window token IDs are now mapped by explicit `outcomes` labels (Up/Down or Yes/No). Ambiguous, duplicate or misaligned mappings fail closed; array position is no longer trusted.
- `best_ask` and `best_bid` now return no quote when the relevant side of the book is empty. Midpoint is a valuation reference, never an executable fill. The paper entry loop requires an actual ask.
- Complete-set arbitrage estimates require both actual asks; a midpoint-only sum is not reported as an arbitrage opportunity.
- ASTF's prior directional probability lift was a heuristic derived from technical features and entry price, not an independently calibrated probability. Directional candidates now fail closed by default with `uncalibrated_probability`; their proxy is labeled diagnostic only. An explicit paper-only opt-in allows tiny discovery probes through the existing exploration budget, never as proven alpha.
- These changes intentionally reduce trade count. A quiet bot is preferable to manufacturing positive expectancy from uncalibrated probabilities or impossible midpoint fills.


## Ziplime package assessment (2026-11-19)
- Evaluated PyPI `ziplime` 2.12.0 and upstream `Limex-com/ziplime`. It is a modern Polars/asyncio Zipline-derived engine with next-bar execution defaults, vectorized causality checks, point-in-time data windows and explicit cost-model enforcement. These ideas are valuable as a research quality checklist.
- It is not a drop-in Polymarket engine: its primary model is bar-based instruments and portfolio simulation, while this repository needs token-level binary CLOB books, queue/depth, partial fills, fees and authoritative resolution. Do not replace our paper ledger or install it as a runtime dependency yet.
- Compatibility: Ziplime requires Python 3.12+ in its upstream pyproject; this repository's Dockerfile and CI explicitly use Python 3.11, so it is currently incompatible without a runtime upgrade. Its GitHub README advertises AGPL-3.0 with an additional permission, while the GitHub repository API and checked-in LICENSE identify GPL-3.0. More importantly, the repository's checked-in END_USER_LICENSE_AGREEMENT says personal trading use is without intent of commercial profit, requires separate licensing for commercial exploitation, and restricts integration into commercial systems. The README's additional-permission link currently points to a missing LICENSE-EXCEPTION.md. This legal ambiguity is a hard stop for using Ziplime in this revenue-oriented project unless the maintainer provides clear written permission and the license materials are reconciled. No package has been installed and no new dependency was added.
- Best use: optionally run it in an isolated research environment to evaluate bar-derived BTC/ETH spot features or compare causal signal calculations. Keep a custom event-driven CLOB replay as the authoritative Polymarket simulator.


### Additional audit: settlement label mapping
- The resolution checker previously interpreted `outcomePrices[0]` as UP and `outcomePrices[1]` as DOWN, and had a token-winner fallback that also assumed array order. It now maps both outcome prices and token winner records by explicit Up/Down or Yes/No labels. Missing or ambiguous labels fail closed rather than settling a potentially reversed position.


## CLOB replay foundation — November 2026
- Added `src/data/market_event_journal.py`: append-only SQLite storage of raw market-channel payloads, exchange timestamps, local receive timestamps, ingestion order and payload hashes.
- The replay helper rebuilds a token book from an initial full snapshot plus price-level deltas, including batched multi-token updates. It can cut off by both exchange time and local receive time, which is required to avoid look-ahead when events arrive late.
- A late older delta is not applied after a newer event; the reconstructed book is marked incomplete. A delta stream without a prior full snapshot is never treated as a complete book.
- Added per-price-level taker-fee accounting to paper book-walk fills. The Polymarket fee curve is price-dependent, so charging one fee at the aggregate VWAP can misstate fees when the simulated fill consumes multiple price levels.
- Added `src/data/market_stream_collector.py`, an optional public-channel WebSocket collector using the documented token subscription, custom market events and 10-second application heartbeat. It can auto-discover ASTF's active BTC/ETH 5m/15m markets and rotate token subscriptions without reconnecting. It reconnects with bounded exponential backoff, forces reconnect after heartbeat silence, and journals per-token gaps after disconnect/unsubscribe. It is not wired into the ASTF signal loop and no collected dataset exists yet.
- Replay now invalidates book state across disconnects and will not mark a book complete until a new full snapshot arrives. Historical exchange-time replay requires a local receive-time cutoff as well; this blocks future-arriving messages from leaking into a past decision.
- Important boundary: this is a paper-data collector only. It does not connect to the authenticated user channel, submit orders, or change the strategy runtime.
- Next order: build a deterministic event-time replay executor with book walking, conservative decision/submit latency and cancel/replace behavior; connect it to the recorded CLOB dataset; compare order-flow imbalance, microprice, liquidity withdrawal, cross-asset lead/lag and near-resolution signals against a no-trade baseline; only then enable calibrated paper candidates.


## Execution fee audit — November 2026
- Corrected the paper fee model to Polymarket's published share-based formula: `fee = shares × feeRate × p × (1-p)`. The prior implementation incorrectly multiplied the per-share fee term by dollar notional, understating fees by a factor related to entry price.
- Added `taker_fee_usd` with five-decimal rounding (sub-increment fees can round to zero), corrected notional conversion, and applied price-level fee treatment to book-walk entries and partial exits.
- Added fee formula tests for share quantities, notional conversion, symmetry, minimum fee and per-share edge.
- Hardened CLOB replay: non-finite levels are ignored; a full snapshot after a connection gap resets the exchange-time baseline. A clean snapshot is the authoritative recovery point.
- Collector reconnects when no WebSocket frame/PONG is received for 25 seconds, rather than trusting an apparently open TCP socket. Exponential backoff resets only after a stable 60-second session.
- Added collector decoding tests for legacy market-channel book events, batched events and heartbeat frames.


## Experimental microstructure features — November 2026
- Added `src/signals/microstructure.py` with deterministic book spread, microprice, top/depth imbalance, best-queue OFI, separate depth deltas, and bid/ask depth-disappearance fractions. Snapshot differences do not prove cancellation.
- Features reject incomplete, one-sided and crossed/locked books. They are computed from current and prior snapshots only; no probability, alpha claim or trading action is produced.
- These features are not yet connected to the live ASTF decision loop. Their usefulness must be tested against timestamped replay, realistic execution costs, and out-of-sample periods before any signal weighting is changed.
- Collector now treats malformed market payloads as skippable input but treats SQLite/storage failures as fatal to the current stream session, forcing reconnect and recording a gap rather than silently dropping data. Dynamic subscription changes are serialized to avoid races with reconnects.


## Buy-fee shares and market-specific fee schedules — November 2026
- Polymarket's published fee formula is share-based. For BUY taker fills, the fee is withheld in shares; for SELL taker fills, the fee is charged in USDC. The earlier paper ledger treated both as a cash fee and overstated the shares received on buys.
- Paper execution now records gross shares, USDC-equivalent fee, fee shares, net shares received and fee-adjusted entry basis. Book-walk fees convert each price-level fee to withheld shares at that level's execution price.
- Durable lifecycle accounting now limits partial exits and settlement to net shares received. Entry fees are included in cost basis and are not subtracted a second time. Legacy databases add/backfill fee-share data and reprice existing realized exit/settlement rows.
- Fee discovery prefers public CLOB market metadata (fd.r / fd.e), falls back to token /fee-rate basis points, respects explicit Gamma feesEnabled=false, and only then uses a marked conservative configuration fallback. The resolved fee rate/exponent is carried through exit management and restart recovery. Explicit zero rates are not overwritten by truthiness fallbacks.
- Five-decimal rounding allows tiny fees to round to zero; no artificial minimum charge is imposed.
- Microstructure OFI now uses the standard best-queue price/size transition calculation. Depth changes are kept separate, and disappearance is not mislabeled as cancellation/withdrawal.
- These fixes improve simulation fidelity, not strategy profitability. The model remains paper-only and still needs recorded data, event-time replay and out-of-sample validation.


## Event-time CLOB replay harness — November 2026
- Added `src/execution/clob_replay.py`. It accepts timestamped paper decision intents, reconstructs the decision-time book using only locally received events up to the decision cutoff, advances by configurable decision/submit latency, and executes against the submit-time ask ladder using the paper book-walk executor.
- Replay rejects incomplete books, disconnect gaps, stale decision/submit books, missing asks, and insufficient depth/slippage-cap failures. It keeps decision-time microstructure features separate from submit-time fill conditions.
- The journal now exposes the receive time of the last book-changing event, so freshness is based on actual book updates rather than unrelated market/trade messages.
- Tests cover latency-induced quote changes, exclusion of events arriving after the submit cutoff, disconnects between decision and submit, and stale books.
- Boundary: this is an offline execution harness. It is not wired into ASTF's signal loop, does not collect data itself, and has no historical strategy results until the collector is run and decisions are generated from a validated strategy.


## Microstructure baseline strategy — November 2026
- Added `src/research/microstructure_replay.py`: generates exploratory paper intents only when normalized best-queue OFI and microprice edge agree, with spread, depth and per-token cooldown filters.
- It consumes book-update receive-time cutoffs from the journal, resets prior-book state across disconnect gaps, and hands intents to the latency-aware CLOB replay engine.
- Tests use synthetic book events to verify signal generation, event-time fills, and no OFI inference across a disconnect.
- This is a baseline for research, not an enabled strategy. Thresholds are not optimized; no live CLOB dataset has been captured in this run; there is no measured hit rate or expectancy. The next meaningful step is to collect real 5m/15m BTC/ETH book data and run chronological walk-forward comparisons against no-trade and simpler baselines.
