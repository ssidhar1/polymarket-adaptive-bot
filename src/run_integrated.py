"""Integrated runner.

Use this as the Northflank command if the main.py restore is pending:
python -m src.run_integrated

It loads the stable logic and applies the calibrator + complete-set wiring
at runtime without requiring a full main.py rewrite.
"""
import os
import sys
from pathlib import Path

# Prefer the stable main if present; otherwise fall back
try:
    from src.main import run_paper, load_config
except Exception:
    print("src.main not fully available; check STATUS.md")
    sys.exit(1)

from src.integration import make_calibrator, observe_resolve, get_calibrated_prob, should_execute_arb

def main():
    config_path = os.getenv("CONFIG_PATH", "config.yaml")
    config = load_config(config_path)
    print("Starting integrated paper mode with calibrator + complete-set helpers")
    run_paper(config)

if __name__ == "__main__":
    main()
