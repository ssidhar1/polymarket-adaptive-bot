"""
Main entry point for Adaptive Short-Term Filter bot.
"""
import argparse
import time
import yaml
import os
from pathlib import Path
from collections import defaultdict, deque
from dotenv import load_dotenv

from src.utils.logging import setup_logging
from src.data.polymarket_client import PolymarketData
from src.data.features import build_features
from src.data.price_feed import PriceFeed
from src.signals.scorer import SignalScorer
from src.risk.manager import RiskManager
from src.learning.adaptive import AdaptiveLearner
from src.execution.paper import PaperExecutor
from src.execution.positions import PositionTracker

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
    prices = PriceFeed()
    scorer = SignalScorer(config)
    risk = RiskManager(config, config.get("bankroll", 1000.0))
    learner = AdaptiveLearner(config)
    executor = PaperExecutor()
    tracker = PositionTracker()

    assets = config.get("markets", {}).get("targets", ["btc", "eth"])
    cycle = 15
    price_history = defaultdict(lambda: deque(maxlen=12))

    logger.info(f"Bankroll ${risk.bankroll:.2f} | assets={assets}")

    while True:
        try:
            if risk.paused:
                logger.warning(f"PAUSED: {risk.pause_reason}")
                time.sleep(60)
                continue

            # Update external prices + momentum
            px = prices.get_prices()
            for asset, p in px.items():
                price_history[asset].append(p)

            scorer.min_score = learner.get_min_score()

            markets = data.get_active_short_term_markets(assets=assets)
            if not markets:
                logger.info("No matching short-term markets")
                time.sleep(cycle)
                continue

            for m in markets[:6]:
                up_token = m.get("_up_token")
                down_token = m.get("_down_token")
                asset = m.get("_asset", "btc")
                if not up_token:
                    continue

                book = data.get_order_book(up_token)
                hist = list(price_history.get(asset, []))
                mom = prices.momentum(asset, hist) if len(hist) >= 2 else 0.0

                features = build_features(m, book, external_momentum=mom)
                signal = scorer.score(features)

                if not signal["pass"]:
                    continue

                mid = features.get("mid_price") or 0.5
                direction = signal["direction"]
                edge = abs(mid - 0.5)
                min_edge = config.get("risk", {}).get("min_edge_after_fees", 0.015)
                if edge < min_edge:
                    continue

                size = risk.size_position(signal["score"])
                if not risk.can_open(size):
                    continue

                # Avoid opening multiple on same market
                if any(p.market_id == (m.get("id") or m.get("slug")) for p in tracker.get_open_list()):
                    continue

                token = up_token if direction == "up" else down_token
                order = executor.place_order(
                    token_id=token,
                    side="buy",
                    price=mid,
                    size_usd=size,
                    market_id=m.get("id") or m.get("slug"),
                )

                tracker.add(
                    order,
                    direction=direction,
                    slug=m.get("slug", ""),
                    end_date=m.get("endDate") or m.get("end_date_iso"),
                )

                logger.info(
                    f"OPEN {direction.upper()} | score={signal['score']:.3f} | "
                    f"mid={mid:.3f} | mom={mom:.2f} | size=${size:.1f} | {m.get('slug','')[:45]}"
                )

            # Simple resolution simulation for paper:
            # After ~2-3 cycles we randomly resolve some positions so the learner gets feedback.
            # In production this would check market closed status + final prices.
            import random
            for pos in list(tracker.get_open_list()):
                age = (time.time() - pos.opened_at.timestamp())
                if age > 90 and random.random() < 0.35:  # simulate some resolve after ~1.5 min
                    # Biased toward truth for paper demo; real system uses actual resolution
                    win_prob = 0.55 if pos.direction == "up" else 0.45  # mild bias for demo
                    res = 1.0 if random.random() < win_prob else 0.0
                    trade = tracker.resolve(pos.order_id, res)
                    if trade:
                        risk.record_pnl(trade["pnl"])
                        learner.log_trade({
                            "score": 0.7,  # would store real score
                            "direction": trade["direction"],
                            "entry_price": trade["entry_price"],
                            "pnl": trade["pnl"],
                            "features": {},
                        })

            summary = tracker.summary()
            logger.info(
                f"Cycle | mkts={len(markets)} | open={summary['open_count']} | "
                f"closed={summary['closed_count']} | pnl=${summary['total_pnl']:.2f} | "
                f"win={summary['win_rate']:.0%} | regime={learner.get_regime()} | "
                f"min_score={learner.get_min_score():.3f} | bankroll=${risk.bankroll:.2f}"
            )

        except Exception as e:
            logger.exception(f"Loop error: {e}")

        time.sleep(cycle)

def run_live(config: dict):
    logger.warning("LIVE mode not enabled. Paper trade first and verify edge.")

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
