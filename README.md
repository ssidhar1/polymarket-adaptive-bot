# ASTF — Adaptive Short-Term Filter

**Aggressive, self-learning paper trader** for Polymarket 5m / 15m BTC & ETH Up/Down markets.

> ~50/50 base rate + dynamic fees. Structural edges + online calibration.

## New in this pass
- **Online calibrated probability model** (`src/learning/calibrated.py`) — shrinks heuristic lifts toward empirical win rates by (hour bucket, remaining time, strength). Used when samples exist.
- **Near-resolution / TWAP-lock sleeve** — stronger boost when remaining time is low and drive is high; can override the uncalibrated gate on strong late setups.
- **Complete-set execution flag** — `execution.execute_complete_set: true` (currently logs intent; paired paper fills next).
- Microprice, cross-asset lag, and cross-window lead-lag remain active.
- Maker preference and tighter exit policy defaults.

## Unique edges
- Calibrated probability (online, sample-aware)
- Near-resolution / TWAP-lock sleeve
- Cross-asset lag + cross-window lead-lag
- Microprice pressure
- Complete-set arb (flag-gated)
- Setup-bucket learning with Wilson intervals
- Regime-conditional pyramiding
- Durable paper ledger + exposure caps

## Quick Start
```bash
git clone https://github.com/ssidhar1/polymarket-adaptive-bot.git
cd polymarket-adaptive-bot
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
python -m src.main --mode paper
```

Watch `PROOF` and `ARB` lines. Directional entries stay fail-closed until the calibrator has samples or you set `allow_heuristic_paper_entries: true`.

Paper first. Live locked.

MIT
