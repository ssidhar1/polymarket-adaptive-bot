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
from src.data.features import build_features
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
    logger.info("=== ASTF PAPER MODE STARTED ===")
    data = PolymarketData()
    scorer = SignalScorer(config)
    risk = RiskManager(config, config.get("bankroll", 1000.0))
    learner = AdaptiveLearner(config)
    executor = PaperExecutor()

    assets = config.get("markets", {}).get("targets", ["btc", "eth"])
    cycle = 20  # seconds

    logger.info(f"Bankroll ${risk.bankroll:.2f} | assets={assets} | min_score starts at {learner.get_min_score():.3f}")

    while True:
        try:
            if risk.paused:
                logger.warning(f"PAUSED: {risk.pause_reason}")
                time.sleep(60)
                continue

            # Dynamic threshold from learner
            scorer.min_score = learner.get_min_score()

            markets = data.get_active_short_term_markets(assets=assets)
            if not markets:
                logger.info("No matching short-term markets right now")
                time.sleep(cycle)
                continue

            for m in markets[:8]:  # limit per cycle
                up_token = m.get("_up_token")
                down_token = m.get("_down_token")
                if not up_token:
                    continue

                book = data.get_order_book(up_token)
                features = build_features(m, book)
                signal = scorer.score(features)

                if not signal["pass"]:
                    continue

                # Edge check after fees (rough)
                mid = features.get("mid_price") or 0.5
                direction = signal["direction"]
                # We buy the side we like; require enough distance from 0.5 after costs
                edge = abs(mid - 0.5) if direction == "up" else abs(0.5 - mid)
                min_edge = config.get("risk", {}).get("min_edge_after_fees", 0.015)
                if edge < min_edge:
                    continue

                size = risk.size_position(signal["score"])
                if not risk.can_open(size):
                    continue

                token = up_token if direction == "up" else down_token
                side = "buy"
                price = mid  # paper uses mid + slippage inside executor

                order = executor.place_order(
                    token_id=token,
                    side=side,
                    price=price,
                    size_usd=size,
                    market_id=m.get("id") or m.get("slug"),
                )

                logger.info(
                    f"SIGNAL {direction.upper()} | score={signal['score']:.3f} | "
                    f"mid={mid:.3f} | size=${size:.1f} | slug={m.get('slug', '')[:40]}"
                )

                # In a full system we would track open positions and resolve later.
                # For now we log the decision; resolution loop can be added next.

            logger.info(
                f"Cycle done | markets={len(markets)} | positions={len(risk.positions)} | "
                f"regime={learner.get_regime()} | min_score={learner.get_min_score():.3f}"
            )

        except Exception as e:
            logger.exception(f"Loop error: {e}")

        time.sleep(cycle)

def run_live(config: dict):
    logger.warning("LIVE mode skeleton only. Paper extensively first.")
    # Future: SecureClient + real signed orders

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
