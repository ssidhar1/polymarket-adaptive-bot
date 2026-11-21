# Integration Status

## Done
- Online calibrated probability model (`src/learning/calibrated.py`)
- Near-resolution / TWAP-lock sleeve in ensemble
- Complete-set execution path (flag-gated)
- Config flags enabled
- One-command integration script

## Apply everything in one step
```bash
git pull
python scripts/apply_integration.py
# review src/main.py
git add src/main.py && git commit -m "Wire calibrator and complete-set" && git push
```
Then redeploy Northflank.

## What it enables
- Empirical calibration after ~15 resolves
- Near-resolution overrides on strong late setups
- Complete-set paper execution when `execute_complete_set: true` and edge > 0.003

## Next after this
1. Let it run 30–50 resolves and watch for `probability_kind=calibrated` and `ARB EXECUTE` lines.
2. Flip `strategy_portfolio.enabled: true` once two sleeves show positive expectancy.
3. Add maker quoting loop (prefer_maker already on) for rebate harvesting.

Paper first. Live locked.
