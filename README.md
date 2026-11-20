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
