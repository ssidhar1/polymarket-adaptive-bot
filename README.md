# Polymarket Adaptive Short-Term Filter (ASTF)

**Self-learning, higher-frequency unsupervised bot** for Polymarket 5-minute and 15-minute crypto Up/Down markets (BTC, ETH).

> **Reality Check**: $1,000 → $150k in 7 months is lottery territory. Most bots on these markets lose money after fees. This system is built for aggressive but controlled edge hunting with hard circuit breakers. **Paper trade extensively first.**

## Current Capabilities
- Live Gamma scanner for active 5m/15m BTC & ETH Up/Down markets
- CLOB order books + Binance momentum feed
- Multi-feature scoring (imbalance, time decay, spread regime, momentum)
- Adaptive threshold + regime detection
- Strict risk manager (size, concurrent, daily loss, max drawdown)
- Paper execution with fees + slippage
- **Real market resolution checking** via Gamma (closed status + outcome prices)
- Persistent trade log (`trades.csv`) + daily summary
- Self-learning loop that receives real outcomes

## Quick Start

```bash
git clone https://github.com/ssidhar1/polymarket-adaptive-bot.git
cd polymarket-adaptive-bot
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml

# Scan current markets
python -m scripts.scan_once

# Run full paper loop
python -m src.main --mode paper

# Later: daily report
python -m scripts.daily_report
```

## Project Structure
```
src/
├── data/
│   ├── polymarket_client.py   # Gamma scanner + CLOB
│   ├── features.py
│   ├── price_feed.py          # External momentum
│   └── resolution.py          # Real closed-market checker
├── signals/scorer.py
├── risk/manager.py
├── learning/adaptive.py
├── execution/
│   ├── paper.py
│   ├── positions.py
│   └── live.py                # Skeleton only
├── utils/
│   ├── logging.py
│   └── persistence.py         # trades.csv + daily summary
└── main.py
```

## Risk Defaults
- 5% of bankroll max per position
- Max 4 concurrent positions
- 8% daily loss → pause
- 15% max drawdown → hard pause
- Minimum edge after fees required to enter

## Status
- **Paper mode**: fully functional end-to-end with real resolution checks and persistent logging
- **Live mode**: intentionally disabled skeleton — only enable after proven paper edge

## Disclaimer
Experimental software. You can (and likely will) lose money if you go live without extensive paper testing. You are fully responsible for any capital used.

MIT License
