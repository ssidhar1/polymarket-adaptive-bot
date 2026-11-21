# Current Status

## Ready (auto-redeploy on commit)
- `src/learning/calibrated.py` — online calibrated probability model
- `src/signals/ensemble.py` — near-resolution sleeve + calibrated_prob support
- `config.example.yaml` — `near_resolution_boost: true`, `execute_complete_set: true`, tighter degen defaults
- README and INTEGRATION.md updated

## Main.py
The core logic is stable. To finish the wiring, apply these four small changes in `src/main.py`:

1. Add import:
   ```python
   from src.learning.calibrated import CalibratedProbabilityModel
   ```
2. After `learner = AdaptiveLearner(config)`:
   ```python
   calibrator = CalibratedProbabilityModel(min_samples=15)
   ```
3. After the main resolve `session_wins += 1` line:
   ```python
   calibrator.observe(trade)
   ```
4. Replace the two `directional_score` calls with calibrated versions (see INTEGRATION.md).

Complete-set execution is already gated by the config flag. Once the calibrator is observed, directional entries can pass with `probability_kind="calibrated"` after ~15 resolves.

Northflank will auto-redeploy on the next commit to main after these changes.

## Next after wiring
- Watch PROOF and ARB EXECUTE logs.
- Flip `strategy_portfolio.enabled: true` once two sleeves show positive paper expectancy.
- Maker quoting layer is the following structural edge.
