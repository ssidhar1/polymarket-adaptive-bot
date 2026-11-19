# ASTF — Adaptive Short-Term Filter

**Aggressive, self-learning paper trader** for Polymarket 5m / 15m BTC & ETH Up/Down markets.

> ~50/50 base rate + dynamic fees. Edge from structure: TWAP-lock, cross-asset lag, cross-window lead-lag, maker rebate, setup-bucket learning, and regime-conditional pyramiding.

## Unique edges
- **TWAP-lock** — vs_open dominates near expiry
- **Cross-asset lag** — BTC informs ETH and vice versa
- **Cross-window lead-lag** — real 5m book mid informs overlapping 15m
- **Conservative paper execution** — aggressive fills pay taker fees and adverse slippage; no unverified maker rebate
- **Setup-bucket learning** — boosts/suppresses by (hour, remaining, vs_open strength)
- **Regime-conditional pyramiding** — half-size second add, once per market, in favorable regime
- Aggressive compounding Kelly in degen + favorable
- Dynamic fees, complete-set arb, PROOF logs, idle-decay, open-position persistence

## Quick Start
```bash
git clone https://github.com/ssidhar1/polymarket-adaptive-bot.git
cd polymarket-adaptive-bot
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
python -m src.main --mode paper
```

Watch `PROOF` lines. Redeploy Northflank after pull.

Paper first. Live locked until expectancy is clear.

Experimental. You can lose money.

MIT
