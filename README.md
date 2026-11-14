# ASTF — Adaptive Short-Term Filter

**Aggressive, self-learning paper trader** for Polymarket 5m / 15m BTC & ETH Up/Down markets.

> **Truth**: These windows resolve ~50/50 long-term. After dynamic taker fees (~1.1–1.75%), most bots lose. This system only fires when momentum + vs-open drift + book pressure agree and the share is still cheap enough for positive EV. Paper extensively before any live capital.

## What it does
- **Dynamic fees** matching Polymarket `crypto_fees_v2` (`rate × p × (1-p)`)
- **vs_open drift** — locks first Binance mid inside the window as a proxy for Chainlink TWAP open
- **Late-window boost** when remaining < 160s, drive is strong, and entry is still cheap
- **Complete-set arbitrage** when Up + Down < 1 after fees
- **Multi-timeframe momentum** (Binance 1m/5m/15m) blended with local history
- **Cross-book imbalance** + time/spread/depth filters
- **Kelly-lite sizing** + hard circuit breakers (daily loss, max drawdown)
- **Self-learning** with idle decay so thresholds don't freeze forever
- **PROOF logs** every ~5 min with session PnL, win rate, and bank delta (no volume required)

## Quick Start
```bash
git clone https://github.com/ssidhar1/polymarket-adaptive-bot.git
cd polymarket-adaptive-bot
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
python -m src.main --mode paper
```

Watch for `PROOF` lines in logs. Redeploy only when you pull code changes.

## Architecture
Scanner (Gamma multi-slug) → Books (CLOB) + Binance MTF + vs_open
→ Features + Ensemble (directional + complete-set)
→ Kelly-lite + risk breakers → Paper fills (dynamic fees)
→ Resolution (Gamma multi-path) → Learner + PROOF logs

## Risk
- Position size, concurrent, daily loss, max drawdown circuit breakers
- Against-momentum and expensive-entry rejects
- Stale position soft (mid) then hard (0.5) exit
- Per-market cooldown

## Status
- Paper mode is fully end-to-end and hardened
- Live mode locked until paper expectancy is clear
- Profitability is not guaranteed — prove it in logs first

## Disclaimer
Experimental. You can lose money. Paper first. No volume needed until edge is real.

MIT
