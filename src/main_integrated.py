"""Integrated entry point.

Run with: python -m src.main_integrated --mode paper
Northflank can point to this instead of src.main if needed.
"""
import os
import sys

# Ensure the original main logic is available
from src.main import run_paper, load_config, main as original_main
from src.integration import make_calibrator, observe_resolve, get_calibrated_prob, should_execute_arb

# Monkey-patch the scoring path if the original main is used
# For full wiring, update src/main.py with the four lines in STATUS.md
# This file documents the integration points.

def main():
    print("Integrated entry ready. Calibrator and complete-set helpers available.")
    print("Update src/main.py with the four lines from STATUS.md, then redeploy.")
    # Fall through to original if present
    if hasattr(sys.modules.get("src.main"), "run_paper"):
        original_main()

if __name__ == "__main__":
    main()
