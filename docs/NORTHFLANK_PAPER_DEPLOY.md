# Northflank paper deployment

## Runtime contract

- Container starts with `python -m src.main --mode paper --config config.yaml`.
- Default mode is paper; the live path remains locked.
- Starting bankroll is $1,000 only when no persisted runtime state exists.
- Runtime state and the SQLite trade log live under `ASTF_DATA_DIR`, default `/app/data`.

## Northflank setup

1. Set the service's deploy branch to `main`.
2. Build from the repository Dockerfile.
3. Mount persistent storage at `/app/data` if the selected Northflank plan supports it. Without persistent storage, bankroll state, open positions, adaptive state, and trade history can be lost when the container is replaced.
4. Keep `DEFAULT_MODE=paper`. The Docker command explicitly selects paper mode regardless.
5. Do not set `PAPER_RESET_BANKROLL` permanently. If intentionally starting a clean $1,000 experiment, back up the existing `/app/data` first, set `PAPER_RESET_BANKROLL=1` for one restart, then remove the variable before any later restart. Resetting discards the previous saved paper state and open-position ledger.
6. Check logs for `ASTF PAPER MODE`, active market discovery, `PROOF`, risk pauses, stale-data skips, and resolved trades.

## Current paper risk limits

- Per-position notional: 4% of bankroll.
- Total open notional: 20%.
- Per-asset open notional: 14%.
- Per-window open notional: 18%.
- Daily loss pause: 5% of peak bankroll.
- Maximum drawdown pause: 12%.
- Paper slippage assumption: 20 bps.
- Market-book freshness gate: 8 seconds.
- Spot-price freshness gate: 15 seconds.
- Kelly sizing remains disabled until a calibrated probability model is explicitly validated and enabled.

## Interpreting results

Paper fills are still an approximation. The current engine applies taker fees and adverse slippage but does not yet model full order-book walking, queue position, or partial fills. Treat paper P&L as research evidence only, not as proof that the same return is executable with real capital.

A stale market is not force-closed at an invented midpoint. It remains unresolved until Gamma reports a decisive outcome; investigate unresolved-position alerts and the open-position ledger if positions accumulate.
