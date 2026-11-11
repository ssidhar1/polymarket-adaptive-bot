"""
ASTF — Adaptive Short-Term Filter
Self-learning multi-signal paper trader for Polymarket 5m/15m crypto markets.
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
from src.signals.ensemble import EnsembleSignal
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
    logger.info("=== ASTF PAPER MODE ===")
    data = PolymarketData()
    prices = PriceFeed()
    resolver = ResolutionChecker()
    ensemble = EnsembleSignal(config)
    risk = RiskManager(config, config.get("bankroll", 1000.0))
    learner = AdaptiveLearner(config)
    executor = PaperExecutor()
    tracker = PositionTracker()
    trade_log = TradeLogger()

    assets = config.get("markets", {}).get("targets", ["btc", "eth"])
    cycle = 12
    price_history = defaultdict(lambda: deque(maxlen=20))
    score_map = {}
    cooldown_until = {}
    cooldown_sec = 90

    logger.info(f"Bankroll ${risk.bankroll:.2f} | assets={assets}")

    while True:
        try:
            if risk.paused:
                logger.warning(f"PAUSED: {risk.pause_reason}")
                time.sleep(45)
                continue

            now = time.time()
            px = prices.get_prices()
            for asset, p in px.items():
                price_history[asset].append(p)

            ensemble.min_score = learner.get_min_score()

            for pos in list(tracker.get_open_list()):
                closed, up_p, down_p = resolver.check(
                    slug=pos.slug or None, market_id=pos.market_id or None
                )
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
                            logger.info(
                                f"RESOLVED {pos.direction.upper()} → {final:.0f} | pnl=${trade['pnl']:.3f}"
                            )
                        continue

                if tracker.is_stale(pos):
                    trade = tracker.resolve(pos.order_id, 0.5)
                    if trade:
                        risk.record_pnl(trade["pnl"])
                        score = score_map.pop(pos.order_id, 0.5)
                        learner.log_trade({
                            "score": score,
                            "direction": trade["direction"],
                            "entry_price": trade["entry_price"],
                            "pnl": trade["pnl"],
                            "features": {},
                        })
                        trade_log.log(trade, score=score)
                        logger.warning(
                            f"STALE EXIT {pos.direction} @ 0.5 | pnl=${trade['pnl']:.3f}"
                        )

            markets = data.get_active_short_term_markets(assets=assets)
            open_n = tracker.open_count()

            if not markets:
                summary = tracker.summary()
                daily = trade_log.daily_summary()
                logger.info(
                    f"No markets | open={summary['open_count']} | "
                    f"today {daily['trades']}t ${daily['pnl']:.2f} | bank=${risk.bankroll:.2f}"
                )
                time.sleep(cycle)
                continue

            for m in markets[:8]:
                up_token = m.get("_up_token")
                down_token = m.get("_down_token")
                asset = m.get("_asset", "btc")
                if not up_token or not down_token:
                    continue

                mid_id = str(m.get("id") or m.get("slug") or "")
                if not mid_id:
                    continue

                if cooldown_until.get(mid_id, 0) > now:
                    continue
                if any(p.market_id == mid_id for p in tracker.get_open_list()):
                    continue

                book_up = data.get_order_book(up_token)
                book_down = data.get_order_book(down_token)
                up_mid = book_up.get("mid")
                down_mid = book_down.get("mid")

                hist = list(price_history.get(asset, []))
                mtf = prices.multi_timeframe_momentum(asset)
                mom = prices.momentum(asset, hist)

                features = build_features(m, book_up, external_momentum=mom)
                signal = ensemble.evaluate(features, up_mid=up_mid, down_mid=down_mid)

                if signal.get("type") == "complete_set":
                    size = risk.size_position(signal["score"], is_exploration=False)
                    if not risk.can_open(size, open_count=open_n):
                        continue
                    if not risk.can_open(size, open_count=open_n + 1):
                        continue
                    for side, token, price in [
                        ("up", up_token, signal["up_price"]),
                        ("down", down_token, signal["down_price"]),
                    ]:
                        order = executor.place_order(token, "buy", price, size, mid_id)
                        tracker.add(
                            order,
                            direction=side,
                            slug=m.get("slug", ""),
                            end_date=m.get("endDate") or m.get("end_date_iso"),
                        )
                        score_map[order["order_id"]] = signal["score"]
                        open_n += 1
                    cooldown_until[mid_id] = now + cooldown_sec
                    logger.info(
                        f"ARB SET | edge={signal['edge']:.3f} | size=${size:.1f} | {m.get('slug','')[:40]}"
                    )
                    continue

                is_expl = False
                if not signal.get("pass"):
                    if (
                        learner.get_regime() == "favorable"
                        and signal.get("score", 0) > ensemble.min_score - 0.08
                    ):
                        is_expl = True
                    else:
                        continue

                direction = signal["direction"]
                mid = features.get("mid_price") or 0.5
                if direction == "down" and down_mid is not None:
                    mid = down_mid
                elif direction == "up" and up_mid is not None:
                    mid = up_mid

                est_prob = signal.get("est_prob")
                size = risk.size_position(
                    confidence=signal["score"],
                    est_prob=est_prob,
                    price=mid,
                    is_exploration=is_expl,
                )
                if size <= 0 or not risk.can_open(size, open_count=open_n):
                    continue

                token = up_token if direction == "up" else down_token
                order = executor.place_order(token, "buy", mid, size, mid_id)
                tracker.add(
                    order,
                    direction=direction,
                    slug=m.get("slug", ""),
                    end_date=m.get("endDate") or m.get("end_date_iso"),
                )
                score_map[order["order_id"]] = signal["score"]
                open_n += 1
                cooldown_until[mid_id] = now + cooldown_sec
                if is_expl:
                    risk.record_exploration(size)

                tag = "EXPL" if is_expl else "DIR"
                logger.info(
                    f"{tag} {direction.upper()} | score={signal['score']:.3f} | "
                    f"mid={mid:.3f} | mom={mom:.2f} "
                    f"(1m={mtf['m1']:.2f} 5m={mtf['m5']:.2f} 15m={mtf['m15']:.2f}) | "
                    f"size=${size:.1f} | {m.get('slug','')[:40]}"
                )

            summary = tracker.summary()
            daily = trade_log.daily_summary()
            logger.info(
                f"Cycle | mkts={len(markets)} | open={summary['open_count']} | "
                f"pnl=${summary['total_pnl']:.2f} | today {daily['trades']}t ${daily['pnl']:.2f} | "
                f"regime={learner.get_regime()} | min_score={learner.get_min_score():.3f} | "
                f"bank=${risk.bankroll:.2f}"
            )

        except Exception as e:
            logger.exception(f"Loop error: {e}")

        time.sleep(cycle)


def run_live(config: dict):
    logger.warning("LIVE mode locked. Prove edge in paper first.")


def main():
    parser = argparse.ArgumentParser(description="ASTF — Adaptive Short-Term Filter")
    parser.add_argument(
        "--mode", choices=["paper", "live"], default=os.getenv("DEFAULT_MODE", "paper")
    )
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    config = load_config(args.config)
    if args.mode == "paper":
        run_paper(config)
    else:
        run_live(config)


if __name__ == "__main__":
    main()
