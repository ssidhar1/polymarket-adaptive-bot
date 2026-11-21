# Current Status

## Ready (auto-redeploy on commit)
- `src/learning/calibrated.py` — online calibrated probability model
- `src/signals/ensemble.py` — near-resolution sleeve + calibrated_prob support
- `src/integration.py` — helper functions for calibrator and complete-set
- `config.example.yaml` — `near_resolution_boost: true`, `execute_complete_set: true`
- README, INTEGRATION.md, STATUS.md updated

## Main.py
The current `src/main.py` is a placeholder. Restore the full version from commit `c4bc5286ac` then apply the four lines from `INTEGRATION.md`:

```bash
git show c4bc5286ac:src/main.py > src/main.py
# then add the four lines (import, instantiate, observe, calibrated_prob)
git add src/main.py && git commit -m "Restore and wire calibrator" && git push
```

Northflank will auto-redeploy after that commit.

## What works once wired
- Empirical calibration after ~15 resolves
- Near-resolution sleeve
- Complete-set paper execution when edge > 0.003

## Next automatic features
- Maker quoting loop (prefer_maker already on)
- Strategy portfolio allocator once two sleeves show positive expectancy
