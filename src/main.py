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
from src.utils.persistence import TradeLogger
from src.data.polymarket_client import PolymarketData
from src.data.features import build_features
from src.data.price_feed import PriceFeed
from src.data.resolution import ResolutionChecker
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
    resolver = ResolutionChecker()
    scorer = SignalScorer(config)
    risk = RiskManager(config, config.get("bankroll", 1000.0))
    learner = AdaptiveLearner(config)
    executor = PaperExecutor()
    tracker = PositionTracker()
    trade_log = TradeLogger()

    assets = config.get("markets", {}).get("targets", ["btc", "eth"])
    cycle = 15
    price_history = defaultdict(lambda: deque(maxlen=12))
    # Keep last score per order so we can log it on close
    score_map = {}

    logger.info(f"Bankroll ${risk.bankroll:.2f} | assets={assets}")

    while True:
        try:
            if risk.paused:
                logger.warning(f"PAUSED: {risk.pause_reason}")
                time.sleep(60)
                continue

            px = prices.get_prices()
            for asset, p in px.items():
                price_history[asset].append(p)

            scorer.min_score = learner.get_min_score()

            markets = data.get_active_short_term_markets(assets=assets)

            # --- Try to resolve open positions with real market status ---
            for pos in list(tracker.get_open_list()):
                closed, up_p, down_p = resolver.check(slug=pos.slug, market_id=pos.market_id)
                if closed and up_p is not None:
                    final = resolver.token_final_price(pos.direction, up_p, down_p)
                    if final is not None:
                        trade = tracker.resolve(pos.order_id, final)
                        if trade:
                            risk.record_pnl(trade["pnl"])
                            score = score_map.pop(pos.order_id, 0.7)
                            learner.log_trade({
                                "score": score,
                                "direction": trade["direction"],
                                "entry_price": trade["entry_price"],
                                "pnl": trade["pnl"],
                                "features": {},
                            })
                            trade_log.log(trade, score=score)
                            logger.info(f"REAL RESOLVE {pos.direction} → {final:.0f} | pnl=${trade['pnl']:.3f}")

            if not markets:
                summary = tracker.summary()
                daily = trade_log.daily_summary()
                logger.info(
                    f"No markets | open={summary['open_count']} | "
                    f"today trades={daily['trades']} pnl=${daily['pnl']:.2f} | "
                    f"bankroll=${risk.bankroll:.2f}"
                )
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

                mid_id = m.get("id") or m.get("slug")
                if any(p.market_id == mid_id for p in tracker.get_open_list()):
                    continue

                token = up_token if direction == "up" else down_token
                order = executor.place_order(
                    token_id=token,
                    side="buy",
                    price=mid,
                    size_usd=size,
                    market_id=mid_id,
                )

                tracker.add(
                    order,
                    direction=direction,
                    slug=m.get("slug", ""),
                    end_date=m.get("endDate") or m.get("end_date_iso"),
                )
                score_map[order["order_id"]] = signal["score"]

                logger.info(
                    f"OPEN {direction.upper()} | score={signal['score']:.3f} | "
                    f"mid={mid:.3f} | mom={mom:.2f} | size=${size:.1f} | {m.get('slug','')[:45]}"
                )

            summary = tracker.summary()
            daily = trade_log.daily_summary()
            logger.info(
                f"Cycle | mkts={len(markets)} | open={summary['open_count']} | "
                f"closed={summary['closed_count']} | total_pnl=${summary['total_pnl']:.2f} | "
                f"today={daily['trades']}t ${daily['pnl']:.2f} | "
                f"regime={learner.get_regime()} | min_score={learner.get_min_score():.3f} | "
                f"bankroll=${risk.bankroll:.2f}"
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
