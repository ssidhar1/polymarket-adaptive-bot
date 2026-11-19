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
- **Calibration gate** — heuristic probabilities cannot activate Kelly sizing by default
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

Paper first. Live locked until expectancy is clear.

Experimental. You can lose money.

MIT
