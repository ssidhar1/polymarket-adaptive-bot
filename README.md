# ASTF — Adaptive Short-Term Filter

**Aggressive, self-learning, multi-signal paper trader** for Polymarket 5m / 15m BTC & ETH Up/Down markets.

> **Truth**: No bot is “super duper profitable” by default. Most short-term Polymarket bots lose after fees. This system hunts structural + directional edges with hard circuit breakers. Paper extensively before any real money.

## What it does
- **Complete-set arbitrage** when Up + Down < ~1 after costs
- **Multi-timeframe momentum** (Binance 1m/5m/15m klines + local blend)
- **Ensemble signals** — momentum, book imbalance, time decay, spread, depth filter
- **Kelly-lite sizing** + exploration budget
- **Hardened resolution** — multi-path Gamma lookup (slug, path, events, winner flags)
- **SQLite trade store** (`trades.db`) with daily + all-time reports
- Adaptive threshold + regime detection

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
Scanner (Gamma) → Books (CLOB) + MTF Momentum (Binance klines)
       ↓
Features + Ensemble (directional + complete-set arb)
       ↓
Kelly-lite sizing + Risk circuit breakers
       ↓
Paper execution → Hardened resolution → Learner + trades.db (SQLite)
```

## Risk
- Max position size, concurrent limits
- Daily loss limit → pause (clears next day)
- Max drawdown → hard pause
- Thin-book filter + per-market cooldown
- Stale position force-exit

## Status
- Paper mode is fully end-to-end
- Live mode remains locked
- Profitability is not guaranteed — prove expectancy in paper first

## Disclaimer
Experimental. You can lose money. Paper first.

MIT
