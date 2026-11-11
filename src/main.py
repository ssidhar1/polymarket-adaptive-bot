"""
Main entry point for Adaptive Short-Term Filter bot.
"""
import argparse
import time
import yaml
import os
from pathlib import Path
from dotenv import load_dotenv

from src.utils.logging import setup_logging
from src.data.polymarket_client import PolymarketData
from src.signals.scorer import SignalScorer
from src.risk.manager import RiskManager
from src.learning.adaptive import AdaptiveLearner
from src.execution.paper import PaperExecutor

load_dotenv()
logger = setup_logging(os.getenv("LOG_LEVEL", "INFO"))

def load_config(path: str = "config.yaml") -> dict:
    if not Path(path).exists():
        path = "config.example.yaml"
    with open(path) as f:
        return yaml.safe_load(f)

def run_paper(config: dict):
    logger.info("Starting PAPER mode")
    data = PolymarketData()
    scorer = SignalScorer(config)
    risk = RiskManager(config, config.get("bankroll", 1000.0))
    learner = AdaptiveLearner(config)
    executor = PaperExecutor()

    logger.info(f"Bankroll: ${risk.bankroll:.2f} | Min score: {learner.get_min_score():.3f}")

    # Main loop skeleton
    while True:
        if risk.paused:
            logger.warning(f"System paused: {risk.pause_reason}")
            time.sleep(60)
            continue

        markets = data.get_active_short_term_markets()
        # In real version: build features, score, decide, size, execute
        # For now we just keep the loop alive and show status

        logger.info(f"Heartbeat | positions={len(risk.positions)} | regime={learner.get_regime()} | min_score={learner.get_min_score():.3f}")
        time.sleep(30)  # 30s cycle for paper

def run_live(config: dict):
    logger.warning("LIVE mode is not fully implemented yet. Use paper mode.")
    # Future: wire SecureClient + real order placement

def main():
    parser = argparse.ArgumentParser(description="Polymarket Adaptive Short-Term Filter")
    parser.add_argument("--mode", choices=["paper", "live"], default=os.getenv("DEFAULT_MODE", "paper"))
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    config = load_config(args.config)

    if args.mode == "paper":
        run_paper(config)
    else:
        run_live(config)

if __name__ == "__main__":
    main()
