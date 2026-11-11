# Polymarket Adaptive Short-Term Filter (ASTF)

**Self-learning, higher-frequency unsupervised bot** for Polymarket 5-minute and 15-minute crypto Up/Down markets (BTC, ETH).

> **Reality Check**: $1,000 → $150k in 7 months is lottery territory. Most bots on these markets lose money after fees. This system is built for aggressive but controlled edge hunting with hard circuit breakers. **Paper trade extensively first.**

## What it does right now
- Scans live Gamma markets for active 5m/15m BTC & ETH Up/Down contracts
- Pulls CLOB order books + external Binance mid for momentum
- Builds features (imbalance, time decay, spread regime, momentum)
- Scores with adaptive threshold
- Sizes positions under risk limits
- Paper-trades and simulates resolution so the learner receives outcomes
- Adjusts entry threshold and regime automatically

## Quick Start

```bash
git clone https://github.com/ssidhar1/polymarket-adaptive-bot.git
cd polymarket-adaptive-bot
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml
# optional: cp .env.example .env

# One-shot scan of current markets
python -m scripts.scan_once

# Run paper loop
python -m src.main --mode paper
```

## Project Structure
```
src/
├── data/
│   ├── polymarket_client.py   # Gamma scanner + CLOB books
│   ├── features.py            # Feature engineering
│   └── price_feed.py          # External BTC/ETH momentum
├── signals/scorer.py          # Multi-feature scoring
├── risk/manager.py            # Sizing + circuit breakers
├── learning/adaptive.py       # Self-calibration + regime
├── execution/
│   ├── paper.py               # Paper fills + fees
│   ├── positions.py           # Open/close tracking
│   └── live.py                # Live skeleton (disabled)
└── main.py                    # Full paper decision loop
```

## Risk Defaults
- 5% bankroll max per position
- Max 4 concurrent
- 8% daily loss → pause
- 15% max drawdown → hard pause
- Minimum edge after fees required

## Status
- Paper mode: functional end-to-end loop with learning feedback
- Live mode: skeleton only — do not enable until paper edge is proven
- Resolution is currently simulated for paper; real closed-market checking is the next production upgrade

## Disclaimer
Experimental software. You can lose money. Start in paper mode. You are fully responsible for any capital used.

MIT License
