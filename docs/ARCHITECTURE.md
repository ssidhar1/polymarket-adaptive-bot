# ASTF Architecture

## Goal
Aggressive but controlled self-learning filter on Polymarket 5m/15m crypto Up/Down markets.

## Data Flow
1. **Scanner** (`PolymarketData`) pulls active markets from Gamma, filters for short-term BTC/ETH Up/Down.
2. **PriceFeed** pulls BTC/ETH mid from Binance for momentum.
3. **Features** combine book imbalance, time-to-expiry, spread regime, external momentum.
4. **Scorer** produces direction + confidence score.
5. **RiskManager** decides size and enforces circuit breakers.
6. **PaperExecutor** (or later LiveExecutor) places the order.
7. **PositionTracker** holds open positions and resolves them.
8. **AdaptiveLearner** receives outcomes and adjusts min_score + regime.

## Risk Controls
- Max % of bankroll per position
- Max concurrent positions
- Daily loss limit → pause
- Max drawdown → hard pause
- Minimum edge after fees required to enter

## Self-Learning
- Rolling window of trade outcomes
- Simple regime detection (favorable / normal / chaotic)
- Dynamic adjustment of entry threshold

## Important Reality
Most unsupervised short-term bots lose money after fees. This system is designed to be honest about that and only trade when multiple conditions align. Paper extensively before any live capital.
