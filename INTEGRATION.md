# Integration Status

## Done
- `src/learning/calibrated.py` — online calibrated probability model
- `src/signals/ensemble.py` — near-resolution sleeve + calibrated_prob support
- `config.example.yaml` — `near_resolution_boost`, `execute_complete_set`, tighter defaults
- README updated

## Remaining (main.py)
Apply these three changes to `src/main.py`:

### 1. Import + instantiate
```python
from src.learning.calibrated import CalibratedProbabilityModel
...
learner = AdaptiveLearner(config)
calibrator = CalibratedProbabilityModel(min_samples=18)
```

### 2. Observe on resolves
After every non-stale `learner.log_trade(...)` block, add:
```python
calibrator.observe(trade)
```

### 3. Pass calibrated probability
Replace the directional_score calls with:
```python
cal_prob = calibrator.calibrate(
    heuristic_prob=0.52,
    hour=datetime.now(timezone.utc).hour,
    remaining_s=float(feat.get("time_remaining_s") or 300),
    strength=vs_open,
)
sig = ensemble.directional_score(feat, preferred_side=..., calibrated_prob=cal_prob)
```

### 4. Complete-set execution
When `config["execution"].get("execute_complete_set")` is true and `arb_watch["edge"] > 0.0035`,
place both paper legs at the actual asks (up and down) using the existing `executor.place_order` + `tracker.add` path.

Once those four points are in, the bot will:
- Use empirical calibration when samples exist
- Run the near-resolution sleeve
- Execute complete-set arbs in paper mode

Redeploy after the main.py update.
