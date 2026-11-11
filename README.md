# ASTF — Adaptive Short-Term Filter

**Aggressive, self-learning, multi-signal paper trader** for Polymarket 5m / 15m BTC & ETH Up/Down markets.

> **Truth**: No bot is “super duper profitable” by default. Most short-term Polymarket bots lose after fees. This system is designed to hunt real edges (structural + directional) as hard as possible while still having hard circuit breakers. Paper extensively before any real money.

## What makes it different
- **Complete-set arbitrage detector** — buys both sides when Up + Down < 1 after costs (structural edge)
- **Multi-signal ensemble** — momentum + book imbalance + time decay + spread regime
- **Kelly-lite position sizing** when a probability estimate exists
- **Exploration budget** — small controlled degen capital when regime is favorable
- **Real resolution checking** via Gamma (not simulated forever)
- **Self-calibrating threshold + regime detection**
- Fast cycle + persistent trade log

## Quick Start

```bash
git clone https://github.com/ssidhar1/polymarket-adaptive-bot.git
cd polymarket-adaptive-bot
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml

python -m scripts.scan_once
python -m src.main --mode paper
python -m scripts.daily_report
```

## Architecture
```
Scanner (Gamma) → Books (CLOB) + Momentum (Binance)
       ↓
Features + Ensemble (directional + complete-set arb)
       ↓
Kelly-lite / confidence sizing + Risk circuit breakers
       ↓
Paper execution → Real resolution → Learner + trades.csv
```

## Risk (still serious)
- Max position size, concurrent limits
- Daily loss limit → pause
- Max drawdown → hard pause
- Exploration budget is capped and separate

## Status
- Paper mode is fully end-to-end and aggressive
- Live mode remains locked on purpose
- The only way this becomes highly profitable is if the combination of structural arb + selective directional trades actually has positive expectancy after fees. That must be proven in paper.

## Disclaimer
Experimental. You can lose money. Do not deposit capital you cannot afford to lose. Paper first.

MIT
