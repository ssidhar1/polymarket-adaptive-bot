# Polymarket Adaptive Short-Term Filter (ASTF)

**Innovative, self-learning, higher-frequency unsupervised bot** for Polymarket 5-minute and 15-minute crypto Up/Down markets (BTC, ETH, etc.).

> **Reality Check**: Turning $1,000 into $150k in 7 months is extremely unlikely. Most bots on these markets lose money after fees. This system is designed for aggressive but controlled edge hunting with strong circuit breakers. Paper trade extensively first.

## Core Philosophy
- Only trade when multiple signals align **and** the market offers positive expected value after fees.
- Self-learning: continuously calibrates thresholds and detects regimes.
- Unsupervised with hard risk limits (daily loss, max drawdown, position size).
- Paper-first architecture.

## Features
- Real-time Polymarket CLOB + Gamma data via official `polymarket-client`
- Multi-feature scoring (momentum, book imbalance, volatility regime, time decay)
- Adaptive probability model + threshold adjustment
- Regime detection (trending / mean-reverting / chaotic)
- Strict risk manager with circuit breakers
- Full trade logging for learning loop
- Paper trading engine (live execution skeleton included)

## Quick Start

### 1. Clone & Install
```bash
git clone https://github.com/ssidhar1/polymarket-adaptive-bot.git
cd polymarket-adaptive-bot
python -m venv venv
source venv/bin/activate  # or Windows equivalent
pip install -r requirements.txt
```

### 2. Configure
```bash
cp .env.example .env
# Edit .env with your private key (for live) and optional settings
cp config.example.yaml config.yaml  # adjust risk parameters
```

### 3. Run Paper Mode (recommended first)
```bash
python -m src.main --mode paper
```

### 4. Live Mode (only after extensive paper testing)
```bash
python -m src.main --mode live
```

## Project Structure
```
src/
├── data/           # Polymarket client + feature engineering
├── signals/        # Scoring engine
├── risk/           # Position sizing + circuit breakers
├── learning/       # Adaptive thresholds + regime detection
├── execution/      # Paper + live order handling
└── utils/          # Logging, helpers
```

## Risk Parameters (default in config.yaml)
- Max position size: 5% of bankroll per market
- Max concurrent positions: 4
- Daily loss limit: 8%
- Max drawdown before pause: 15%
- Min edge after fees required to enter

## Disclaimer
This is experimental software. Trading involves substantial risk of loss. Past performance (even in paper) does not guarantee future results. You are solely responsible for any capital you deploy. Start with paper trading and very small size.

## License
MIT
