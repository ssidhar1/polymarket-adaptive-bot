# ASTF — Adaptive Short-Term Filter

**Aggressive, self-learning paper trader** for Polymarket 5m / 15m BTC & ETH Up/Down markets.

> **Reality**: ~50/50 base rate + dynamic taker fees. Edge must come from structure (TWAP lock, cross-asset lag, maker rebate, setup-specific learning), not generic momentum.

## Unique edges now in the code
- **TWAP-lock scoring** — vs_open weighted much higher near expiry (resolution is Chainlink 60s TWAP)
- **Cross-asset lag** — BTC vs_open informs ETH (and vice versa) when one is leading
- **Maker-rebate adjusted edge** — effective fee reduced ~20% when prefer_maker (closer to real economics)
- **Setup-bucket learning** — tracks win rate by (hour, remaining bucket, vs_open strength) and boosts/suppresses those combinations
- **Aggressive compounding Kelly** — higher fraction in degen + favorable regime
- Dynamic fees, complete-set arb, PROOF logs, idle-decay learner, open-position persistence

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

## Status
Paper mode end-to-end. Live locked until paper expectancy is clear. No volume required to judge from logs.

Experimental. You can lose money. Paper first.

MIT
