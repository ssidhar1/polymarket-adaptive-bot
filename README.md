# ASTF — Adaptive Short-Term Filter

**Aggressive, self-learning paper trader** for Polymarket 5m / 15m BTC & ETH Up/Down markets.

> ~50/50 base rate + dynamic fees. Edge from structure: TWAP-lock, cross-asset lag, cross-window lead-lag, maker rebate, setup-bucket learning, and regime-conditional pyramiding.

## Unique edges
- **Window-open reference** — opening-price context is captured only near the actual market boundary; a mid-window restart is not mislabeled as the open
- **Cross-asset lag** — BTC informs ETH and vice versa
- **Cross-window lead-lag** — real 5m book mid informs overlapping 15m
- **Conservative paper execution** — aggressive fills pay taker fees and configured adverse slippage; no unverified maker rebate
- **Microprice pressure** — top-of-book queue imbalance contributes a bounded experimental feature
- **Freshness gates** — stale Binance spot prices and stale order-book snapshots are rejected
- **Portfolio caps** — total, per-asset, and per-window reserved notional limits
- **Calibration gate** — heuristic probability lifts cannot qualify directional entries by default; optional probes are explicitly labeled and capped
- **Complete-set safety** — arb opportunities are logged but not executed until paired-leg atomicity exists
- **Setup-bucket learning** — boosts/suppresses by (hour, remaining, vs_open strength)
- **Regime-conditional pyramiding** — half-size second add, once per market, in favorable regime
- Conservative fractional sizing until out-of-sample probability calibration is proven
- Market-specific fee schedules, complete-set opportunity watch (no paired execution), PROOF logs, idle-decay, open-position persistence

## Quick Start
```bash
git clone https://github.com/ssidhar1/polymarket-adaptive-bot.git
cd polymarket-adaptive-bot
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
python -m src.main --mode paper
```

Watch `PROOF`, `ARB WATCH`, stale-data, and risk-pause logs. Mount a persistent volume at `/app/data` before testing; without it, paper state can be lost on redeploy. The default starting bankroll is $1,000 only when no prior state exists. Set `PAPER_RESET_BANKROLL=1` for one restart if you deliberately want to reset, then remove that variable before the next restart.

Paper first. Live locked until expectancy is clear. Directional paper entries remain fail-closed until a calibrated probability model is connected; midpoint-only quotes are never treated as executable fills.

Experimental. You can lose money.

MIT


## Live execution status (dormant)

A separately tested live adapter exists in `src/execution/live.py`, using the official
`polymarket-client` Python SDK. It is **not connected to the ASTF signal loop**.
The Dockerfile and `main.py` continue to run/refuse anything except paper mode.

The adapter currently supports only an explicitly approved, passive BUY limit order.
It requires all of the following: `live_execution.enabled: true`,
`ASTF_ENABLE_LIVE_TRADING=YES`, `ASTF_LIVE_CONFIRMATION=I_ACCEPT_REAL_MONEY_RISK`,
`DEFAULT_MODE=live`, a per-order `SUBMIT_ONE_LIVE_ORDER` approval, an explicit EOA
wallet matching the private key, a fresh uncrossed book, existing balance and allowance,
and configured order/exposure limits. It does not submit approvals. A timed-out submit
is marked `UNKNOWN` and cannot be retried under the same intent key.

This is a guarded adapter, **not authorization to trade** and not a claim of production
readiness. Before wiring it into the strategy, implement and test exchange-state
reconciliation, partial-fill accounting, cancellation and expiry handling, position
exits/settlement, crash recovery, rate-limit handling, and end-to-end testnet or
mock-server scenarios. No live order is submitted by the current application.


## Unified strategy engine (in progress)

This repository is the canonical paper-trading runtime for multiple independent
Polymarket strategies. See [docs/UNIFIED_MASTER_PLAN.md](docs/UNIFIED_MASTER_PLAN.md).

The shared strategy contract and settlement-only strategy capital allocator are
now available under `src/strategies/`. They are foundational modules and are
**not yet wired into the existing ASTF execution loop**. The example configuration
documents the planned strategy sleeves but keeps `strategy_portfolio.enabled: false`
until persistence, lifecycle integration, and end-to-end replay tests are complete.

Planned research sleeves include:
- ASTF intraday BTC/ETH 5m and 15m
- Weekly price-range / threshold markets
- Evidence-verified near-resolution opportunities
- Weather-station markets using authoritative station data
- Cross-market arbitrage (observation-only until safe paired execution exists)
- Information-latency and settlement intelligence

Only realized, settled net paper P&L may compound a strategy sleeve. Open-position
mark-to-market gains do not increase deployable capital. All strategies share
portfolio-wide exposure and drawdown controls. Paper mode remains the only supported
runtime; no live orders are connected.


### Weekly strategy research status

- Added paginated Gamma/CLOB discovery for binary BTC/ETH price markets resolving within seven days.
- Candidate token mapping uses the explicit outcome labels; asks and top-five depth are read from CLOB. Fees and slippage are estimated, not assumed free.
- Fair probability intentionally remains unknown. The lognormal model is a research primitive only; it is not calibrated or connected to live inputs.
- Chronological walk-forward scoring rejects look-ahead but does not train models. No weekly strategy paper orders are enabled.


## CLOB event journal (paper research foundation)

`src/data/market_event_journal.py` adds an append-only SQLite journal for public
market-channel events. It preserves the raw JSON payload, SHA-256 payload hash,
exchange timestamp, local receive timestamp, and ingestion sequence. Its replay
helper reconstructs L2 books from a full `book` snapshot plus subsequent
`price_change` deltas, supports receive-time cutoffs to prevent look-ahead,
handles multi-token delta batches, and marks books incomplete if a late older
delta arrives after a newer event.

A separate public-channel collector is available. For the bot's BTC/ETH 5m/15m
markets, let it discover and rotate subscriptions automatically:
```bash
python -m src.data.market_stream_collector --discover-short-term --db data/clob_events.sqlite
```
For a fixed token set:
```bash
python -m src.data.market_stream_collector --tokens TOKEN_ID_1,TOKEN_ID_2 --db data/clob_events.sqlite
```
It sends the documented subscription and application heartbeat, journals raw events,
and records a connection gap after disconnect. Replay invalidates the book across
that gap until a new full snapshot arrives. Historical exchange-time replay requires
a local receive-time cutoff as well, preventing late-arriving data from leaking into
the simulated decision. The collector is an optional data process; it is **not yet
wired into ASTF's runtime signal loop**, and no collected dataset or performance
claim exists yet.


## Fee model correction

The paper fee helper follows Polymarket's published share-based formula:
`fee_usd = shares × fee_rate × price × (1 - price)`. Do not multiply the
price-dependent fee-per-share term by dollar notional directly. Fees are
rounded to five decimal places; amounts below the rounding increment can be
zero. Public CLOB market metadata is queried for the market fee coefficient
and exponent, with the token fee-rate endpoint as fallback. Explicit fee-free
markets remain fee-free; a clearly marked conservative configuration fallback
is used only when public metadata cannot be resolved.

CLOB V2 fees are charged in USDC at match time. The paper executor includes
that fee in the cash cost basis while retaining the full purchased share
quantity. An explicit legacy V1 `shares` mode remains available for historical
fills; the durable ledger stores the collection mode per entry, migrates older
rows safely, and reconciles realized P&L without double-counting fees.


## Experimental L2 features

`src/signals/microstructure.py` computes book spread, microprice, top/depth
imbalance, standard best-queue order-flow imbalance (OFI), depth deltas, and
observed depth-disappearance fractions from current and prior snapshots. A
snapshot alone cannot tell whether disappearing size was canceled or executed,
so the feature is not labeled as cancellation/withdrawal. Incomplete, one-sided
and crossed books are rejected. These research features are not yet connected
to the strategy runtime and do not constitute evidence of predictive edge.


## Event-time paper replay

`src/execution/clob_replay.py` replays supplied paper decision intents against
the public CLOB event journal. Decisions are timestamped using local receive
time; the engine reads only events received by the decision cutoff, advances
to the simulated submit cutoff, rejects stale/incomplete books, and uses the
submit-time ask ladder for a depth-aware paper fill. It records decision-time
microstructure features separately from submit-time execution conditions.

This is an execution/research harness, not a strategy or profitability claim.
It is not connected to the live ASTF loop and requires an actual journal dataset
plus externally supplied decision intents. A supplied resolution price is used
only for offline paper scoring.


## Microstructure research baseline

`src/research/microstructure_replay.py` generates a simple exploratory
baseline: buy an outcome token only when normalized best-queue OFI and
microprice edge agree, subject to spread, depth and cooldown filters. It then
passes those timestamped intents into the event-time replay engine. The
thresholds are research parameters, not tuned or validated. This baseline is
not connected to the production signal loop and must be compared with a
no-trade baseline and walk-forward holdouts before any deployment decision.


Run the baseline against a locally collected journal:
```bash
python -m src.research.microstructure_replay \
  --db data/clob_events.sqlite \
  --tokens TOKEN_ID_1,TOKEN_ID_2 \
  --fee-rate 0.07 --latency-ms 250 --slippage-bps 8
```
The production market-discovery path queries CLOB V2 market info by condition
ID and records whether the fee rate was verified. The standalone CLI receives
only token IDs, so it uses the marked `--fee-rate` fallback unless a fee
schedule is supplied through the Python API. It deliberately does not trust
the legacy `/fee-rate` endpoint for V2. CLOB V2 uses USDC fee collection by
default; `--fee-collection shares` is an explicit legacy V1 override. Without externally supplied resolution outcomes, the report
contains fills and open paper positions but no realized strategy P&L. Do not interpret candidate
counts or mark-to-market values as validated returns.
