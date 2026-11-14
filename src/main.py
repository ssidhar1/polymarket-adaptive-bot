"""
ASTF — Adaptive Short-Term Filter
Self-learning multi-signal paper trader for Polymarket 5m/15m crypto markets.
"""
import argparse
import signal as os_signal
import time
import yaml
import os
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict, deque
from dotenv import load_dotenv

from src.utils.logging import setup_logging
from src.utils.persistence import TradeLogger
from src.utils.state import StateStore
from src.data.polymarket_client import PolymarketData
from src.data.features import build_features, best_ask
from src.data.price_feed import PriceFeed
from src.data.resolution import ResolutionChecker
from src.signals.ensemble import EnsembleSignal
from src.risk.manager import RiskManager
from src.learning.adaptive import AdaptiveLearner
from src.execution.paper import PaperExecutor
from src.execution.positions import PositionTracker

load_dotenv()
logger = setup_logging(os.getenv("LOG_LEVEL", "INFO"))


def _data_dir() -> Path:
    d = Path(os.getenv("ASTF_DATA_DIR", "data"))
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d

_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    logger.info(f"Shutdown signal received ({signum}) — finishing cycle...")
    _shutdown = True


def load_config(path: str = "config.yaml") -> dict:
    if not Path(path).exists():
        path = "config.example.yaml"
    with open(path) as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        raise SystemExit(f"Invalid config at {path}")
    return cfg


def _timing_meta(m: dict, feat: dict, score: float, edge: float, mom: float, regime: str, trade_type: str) -> dict:
    now = datetime.now(timezone.utc)
    window = m.get("_window") or ""
    if not window:
        slug = (m.get("slug") or "").lower()
        if "5m" in slug:
            window = "5m"
        elif "15m" in slug:
            window = "15m"
    return {
        "asset": m.get("_asset") or "",
        "window": window,
        "hour_utc": now.hour,
        "weekday_utc": now.weekday(),
        "time_remaining_s": float(feat.get("time_remaining_s") or 0),
        "time_remaining_pct": float(feat.get("time_remaining_pct") or 0),
        "score": float(score or 0),
        "edge_after_fees": float(edge or 0),
        "momentum": float(mom or 0),
        "regime": regime or "",
        "trade_type": trade_type,
    }


def _save_state(state: StateStore, risk: RiskManager, learner: AdaptiveLearner, tracker: PositionTracker):
    try:
        state.save({
            "bankroll": risk.bankroll,
            "peak_bankroll": risk.peak_bankroll,
            "learner": learner.export_state(),
            "open_count": tracker.open_count(),
            "open_positions": tracker.export_open(),
        })
    except Exception as e:
        logger.warning(f"State save failed: {e}")


def _prune_cooldowns(cooldown_until: dict, now: float):
    dead = [k for k, t in cooldown_until.items() if t <= now]
    for k in dead:
        del cooldown_until[k]


def run_paper(config: dict):
    global _shutdown
    os_signal.signal(os_signal.SIGINT, _handle_signal)
    os_signal.signal(os_signal.SIGTERM, _handle_signal)

    logger.info("=== ASTF PAPER MODE ===")
    data = PolymarketData()
    prices = PriceFeed()
    resolver = ResolutionChecker()
    ensemble = EnsembleSignal(config)
    state = StateStore(path=_data_dir() / "runtime_state.json")
    saved = state.load()

    if os.getenv("PAPER_RESET_BANKROLL", "").lower() in ("1", "true", "yes"):
        start_bank = float(config.get("bankroll", 1000.0))
        logger.warning(f"PAPER_RESET_BANKROLL: starting fresh at ${start_bank:.2f}")
        saved = {}
    else:
        start_bank = float(saved.get("bankroll", config.get("bankroll", 1000.0)))

    risk = RiskManager(config, start_bank)
    if saved.get("peak_bankroll") and not os.getenv("PAPER_RESET_BANKROLL"):
        try:
            risk.peak_bankroll = max(risk.bankroll, float(saved["peak_bankroll"]))
        except (TypeError, ValueError):
            pass

    risk.clear_daily_pause("service restart")

    learner = AdaptiveLearner(config)
    learner.import_state(saved.get("learner") or {})
    if learner.get_min_score() > 0.70:
        logger.warning(
            f"Soft-capping restored min_score {learner.get_min_score():.3f} → 0.65"
        )
        learner.current_min_score = 0.65
        if learner.regime == "chaotic":
            learner.regime = "normal"

    fee_rate = float(config.get("signals", {}).get("fee_rate", 0.07))
    executor = PaperExecutor(fee_rate=fee_rate)
    tracker = PositionTracker()
    if saved.get("open_positions") and not os.getenv("PAPER_RESET_BANKROLL"):
        try:
            tracker.import_open(saved["open_positions"])
        except Exception as e:
            logger.warning(f"Open position restore failed: {e}")

    trade_log = TradeLogger(path=_data_dir() / "trades.db")

    assets = config.get("markets", {}).get("targets", ["btc", "eth"]) or ["btc", "eth"]
    cycle = max(5, int(config.get("execution", {}).get("cycle_seconds", 10) or 10))
    price_history = defaultdict(lambda: deque(maxlen=20))
    cooldown_until = {}
    window_open_px = {}
    cooldown_sec = max(15, int(config.get("execution", {}).get("cooldown_seconds", 60) or 60))
    max_per_cycle = max(1, int(config.get("execution", {}).get("max_markets_per_cycle", 12) or 12))
    cycles_since_save = 0
    cycles_since_timing = 0
    last_pause_log = 0.0
    consecutive_errors = 0
    session_start_bank = float(risk.bankroll)
    session_start_ts = time.time()
    session_resolves = 0
    session_wins = 0
    session_pnl = 0.0
    cycles_since_proof = 0

    logger.info(
        f"Bankroll ${risk.bankroll:.2f} | peak=${risk.peak_bankroll:.2f} | "
        f"open_restored={tracker.open_count()} | assets={assets} | "
        f"min_score={learner.get_min_score():.3f} | regime={learner.get_regime()} | "
        f"degen={config.get('signals', {}).get('degen_mode', True)} | fee_sched~{fee_rate:.2f}"
    )

    while not _shutdown:
        try:
            risk.update_bankroll(risk.bankroll)

            if risk.paused:
                now_t = time.time()
                if now_t - last_pause_log > 600:
                    logger.warning(
                        f"PAUSED: {risk.pause_reason} | bank=${risk.bankroll:.2f} "
                        f"peak=${risk.peak_bankroll:.2f} | sleeping (clears next UTC day or Restart)"
                    )
                    last_pause_log = now_t
                time.sleep(120)
                continue

            now = time.time()
            _prune_cooldowns(cooldown_until, now)
            if len(window_open_px) > 64:
                for k in list(window_open_px.keys())[: len(window_open_px) // 2]:
                    del window_open_px[k]

            try:
                px = prices.get_prices()
                for asset, p in px.items():
                    if p and p > 0:
                        price_history[asset].append(p)
            except Exception as e:
                logger.warning(f"Price feed error: {e}")

            ensemble.min_score = learner.get_min_score()
            regime = learner.get_regime()

            for pos in list(tracker.get_open_list()):
                try:
                    closed, up_p, down_p = resolver.check(
                        slug=pos.slug or None,
                        market_id=pos.market_id or None,
                    )
                    if closed and up_p is not None:
                        final = resolver.token_final_price(pos.direction, up_p, down_p)
                        if final is not None:
                            trade = tracker.resolve(pos.order_id, final)
                            if trade:
                                risk.record_pnl(trade["pnl"])
                                learner.log_trade({
                                    "score": trade.get("score", 0.7),
                                    "direction": trade["direction"],
                                    "entry_price": trade["entry_price"],
                                    "pnl": trade["pnl"],
                                    "stale": False,
                                })
                                trade_log.log(trade, score=trade.get("score", 0))
                                session_resolves += 1
                                session_pnl += float(trade["pnl"])
                                if float(trade["pnl"]) > 0:
                                    session_wins += 1
                                logger.info(
                                    f"RESOLVED {pos.direction.upper()} → {final:.0f} | "
                                    f"pnl=${trade['pnl']:.3f} | "
                                    f"h={trade.get('hour_utc')} wd={trade.get('weekday_utc')} "
                                    f"win={trade.get('window')} t_left={trade.get('time_remaining_s', 0):.0f}s"
                                )
                            continue

                    stage = tracker.stale_stage(
                        pos,
                        grace_seconds=int(config.get("execution", {}).get("stale_grace_seconds", 300)),
                        max_hold_minutes=int(config.get("execution", {}).get("stale_max_hold_minutes", 35)),
                    )
                    if stage == "soft":
                        mid_px = None
                        try:
                            mid_px = data.get_mid_price(pos.token_id)
                        except Exception:
                            mid_px = None
                        if mid_px is not None and 0.02 < float(mid_px) < 0.98:
                            trade = tracker.resolve(pos.order_id, float(mid_px))
                            tag = f"mid={float(mid_px):.3f}"
                        else:
                            continue
                        if trade:
                            risk.record_pnl(trade["pnl"])
                            learner.log_trade({
                                "score": trade.get("score", 0.5),
                                "direction": trade["direction"],
                                "entry_price": trade["entry_price"],
                                "pnl": trade["pnl"],
                                "stale": True,
                            })
                            trade_log.log(trade, score=trade.get("score", 0))
                            session_resolves += 1
                            session_pnl += float(trade["pnl"])
                            if float(trade["pnl"]) > 0:
                                session_wins += 1
                            logger.warning(
                                f"STALE SOFT {pos.direction} @ {tag} | pnl=${trade['pnl']:.3f} | "
                                f"h={trade.get('hour_utc')} win={trade.get('window')}"
                            )
                    elif stage == "hard":
                        trade = tracker.resolve(pos.order_id, 0.5)
                        if trade:
                            risk.record_pnl(trade["pnl"])
                            learner.log_trade({
                                "score": trade.get("score", 0.5),
                                "direction": trade["direction"],
                                "entry_price": trade["entry_price"],
                                "pnl": trade["pnl"],
                                "stale": True,
                            })
                            trade_log.log(trade, score=trade.get("score", 0))
                            session_resolves += 1
                            session_pnl += float(trade["pnl"])
                            if float(trade["pnl"]) > 0:
                                session_wins += 1
                            logger.warning(
                                f"STALE HARD {pos.direction} @ 0.5 | pnl=${trade['pnl']:.3f} | "
                                f"h={trade.get('hour_utc')} win={trade.get('window')}"
                            )
                except Exception as e:
                    logger.warning(f"Resolve error for {getattr(pos, 'order_id', '?')}: {e}")

            try:
                markets = data.get_active_short_term_markets(assets=assets)
            except Exception as e:
                logger.warning(f"Market discovery failed: {e}")
                markets = []

            open_n = tracker.open_count()
            trades_this_cycle = 0

            if not markets:
                summary = tracker.summary()
                daily = trade_log.daily_summary()
                logger.info(
                    f"No markets | open={summary['open_count']} | "
                    f"today {daily['trades']}t ${daily['pnl']:.2f} | bank=${risk.bankroll:.2f}"
                )
            else:
                for m in markets[:max_per_cycle]:
                    try:
                        up_token = m.get("_up_token")
                        down_token = m.get("_down_token")
                        asset = m.get("_asset", "btc")
                        if not up_token or not down_token:
                            continue

                        slug = m.get("slug") or ""
                        mid_id = slug or str(m.get("id") or "")
                        if not mid_id:
                            continue

                        if cooldown_until.get(mid_id, 0) > now:
                            continue
                        if any(
                            (p.slug and p.slug == slug) or p.market_id == mid_id
                            for p in tracker.get_open_list()
                        ):
                            continue

                        book_up = data.get_order_book(up_token)
                        book_down = data.get_order_book(down_token)
                        up_mid = book_up.get("mid")
                        down_mid = book_down.get("mid")
                        up_ask = book_up.get("best_ask") or best_ask(book_up)
                        down_ask = book_down.get("best_ask") or best_ask(book_down)

                        if (up_ask is None and up_mid is None) or (
                            down_ask is None and down_mid is None
                        ):
                            continue

                        hist = list(price_history.get(asset, []))
                        mtf = prices.multi_timeframe_momentum(asset)
                        mom = prices.momentum(asset, hist)

                        try:
                            live_px = float((prices.cache or {}).get(asset) or 0)
                            if live_px <= 0:
                                live_px = float((prices.get_prices() or {}).get(asset) or 0)
                        except Exception:
                            live_px = 0.0
                        if slug and live_px > 0 and slug not in window_open_px:
                            rem = float(m.get("_remaining_s") or 0)
                            win_s = float(m.get("_window_seconds") or 300)
                            if rem <= win_s + 5:
                                window_open_px[slug] = live_px
                        open_px = window_open_px.get(slug)
                        vs_open = 0.0
                        if open_px and open_px > 0 and live_px > 0:
                            vs_open = max(-1.0, min(1.0, (live_px - open_px) / open_px * 150))

                        feat_up = build_features(
                            m, book_up, external_momentum=mom, side="up",
                            book_other=book_down, vs_open=vs_open,
                        )
                        feat_dn = build_features(
                            m, book_down, external_momentum=mom, side="down",
                            book_other=book_up, vs_open=vs_open,
                        )

                        arb = ensemble.complete_set_arb(
                            up_mid, down_mid, up_ask=up_ask, down_ask=down_ask
                        )
                        if arb:
                            budget = risk.size_position(
                                arb["score"],
                                edge_after_fees=arb.get("edge"),
                            )
                            up_px = float(arb["up_price"])
                            dn_px = float(arb["down_price"])
                            pair_cost = up_px + dn_px
                            if budget > 0 and pair_cost > 0:
                                n_shares = budget / pair_cost
                                size_up = n_shares * up_px
                                size_dn = n_shares * dn_px
                                if (
                                    risk.can_open(size_up, open_count=open_n)
                                    and risk.can_open(size_dn, open_count=open_n + 1)
                                ):
                                    meta = _timing_meta(
                                        m, feat_up, arb["score"], arb.get("edge") or 0,
                                        mom, regime, "arb",
                                    )
                                    legs_ok = True
                                    for side, token, price, sz in [
                                        ("up", up_token, up_px, size_up),
                                        ("down", down_token, dn_px, size_dn),
                                    ]:
                                        order = executor.place_order(
                                            token, "buy", price, sz, mid_id,
                                            already_aggressive=True,
                                        )
                                        if not order:
                                            legs_ok = False
                                            break
                                        tracker.add(
                                            order, direction=side, slug=slug,
                                            end_date=m.get("endDate") or m.get("end_date_iso"),
                                            meta=meta,
                                        )
                                        open_n += 1
                                    if legs_ok:
                                        cooldown_until[mid_id] = now + cooldown_sec
                                        trades_this_cycle += 1
                                        logger.info(
                                            f"ARB SET | edge={arb['edge']:.3f} | "
                                            f"shares≈{n_shares:.1f} | spend=${size_up+size_dn:.1f} | "
                                            f"h={meta['hour_utc']} win={meta['window']} | {slug[:40]}"
                                        )
                                    continue

                        sig_up = ensemble.directional_score(feat_up, preferred_side="up")
                        sig_dn = ensemble.directional_score(feat_dn, preferred_side="down")

                        candidates = []
                        for sig, feat, token, entry_px in [
                            (sig_up, feat_up, up_token, up_ask if up_ask is not None else up_mid),
                            (sig_dn, feat_dn, down_token, down_ask if down_ask is not None else down_mid),
                        ]:
                            if entry_px is None:
                                continue
                            try:
                                entry_f = float(entry_px)
                            except (TypeError, ValueError):
                                continue
                            if entry_f <= 0 or entry_f >= 1:
                                continue
                            if not sig.get("pass") and not (
                                regime == "favorable"
                                and sig.get("score", 0) > ensemble.min_score - 0.06
                                and (sig.get("edge_after_fees") or 0) > 0.01
                            ):
                                continue
                            candidates.append((sig, feat, token, entry_f))

                        if not candidates:
                            continue

                        candidates.sort(
                            key=lambda x: (
                                x[0].get("edge_after_fees") or 0,
                                x[0].get("score", 0),
                            ),
                            reverse=True,
                        )
                        sig, feat, token, entry = candidates[0]

                        is_expl = not sig.get("pass")
                        direction = sig["direction"]
                        edge = sig.get("edge_after_fees")

                        size = risk.size_position(
                            confidence=sig["score"],
                            est_prob=sig.get("est_prob"),
                            price=entry,
                            is_exploration=is_expl,
                            edge_after_fees=edge,
                        )
                        if size <= 0 or not risk.can_open(size, open_count=open_n):
                            continue

                        trade_type = "expl" if is_expl else "dir"
                        meta = _timing_meta(
                            m, feat, sig["score"], edge or 0, mom, regime, trade_type
                        )
                        order = executor.place_order(
                            token, "buy", entry, size, mid_id, already_aggressive=True
                        )
                        if not order:
                            continue
                        tracker.add(
                            order, direction=direction, slug=slug,
                            end_date=m.get("endDate") or m.get("end_date_iso"),
                            meta=meta,
                        )
                        open_n += 1
                        trades_this_cycle += 1
                        cooldown_until[mid_id] = now + cooldown_sec
                        if is_expl:
                            risk.record_exploration(size)

                        tag = "EXPL" if is_expl else "DIR"
                        logger.info(
                            f"{tag} {direction.upper()} | score={sig['score']:.3f} | "
                            f"entry={entry:.3f} | edge={edge if edge is not None else 0:.3f} | "
                            f"mom={mom:.2f} vs_open={vs_open:.2f} "
                            f"(1m={mtf['m1']:.2f} 5m={mtf['m5']:.2f} 15m={mtf['m15']:.2f}) | "
                            f"size=${size:.1f} | h={meta['hour_utc']} wd={meta['weekday_utc']} "
                            f"win={meta['window']} t_left={meta['time_remaining_s']:.0f}s | {slug[:40]}"
                        )
                    except Exception as e:
                        logger.warning(f"Market cycle error ({m.get('slug', '?')[:30]}): {e}")
                        continue

                rejects = ensemble.pop_reject_summary()
                reject_str = ""
                if rejects:
                    top = sorted(rejects.items(), key=lambda x: -x[1])[:5]
                    reject_str = " | rej " + ",".join(f"{k}={v}" for k, v in top)

                summary = tracker.summary()
                daily = trade_log.daily_summary()
                logger.info(
                    f"Cycle | mkts={len(markets)} | opened={trades_this_cycle} | "
                    f"open={summary['open_count']} | pnl=${summary['total_pnl']:.2f} | "
                    f"today {daily['trades']}t ${daily['pnl']:.2f} | "
                    f"regime={regime} | min_score={learner.get_min_score():.3f} | "
                    f"bank=${risk.bankroll:.2f}{reject_str}"
                )

            learner.on_cycle(opened=trades_this_cycle)

            cycles_since_proof += 1
            if cycles_since_proof >= 30:
                cycles_since_proof = 0
                uptime_h = (time.time() - session_start_ts) / 3600.0
                wr = (session_wins / session_resolves) if session_resolves else 0.0
                delta = risk.bankroll - session_start_bank
                logger.info(
                    f"PROOF | session_pnl=${session_pnl:.2f} | bank_delta=${delta:.2f} | "
                    f"bank=${risk.bankroll:.2f} start=${session_start_bank:.2f} | "
                    f"resolves={session_resolves} wins={session_wins} wr={wr:.1%} | "
                    f"uptime_h={uptime_h:.2f} | regime={learner.get_regime()} "
                    f"min_score={learner.get_min_score():.3f}"
                )

            cycles_since_save += 1
            cycles_since_timing += 1
            if cycles_since_save >= 5:
                _save_state(state, risk, learner, tracker)
                cycles_since_save = 0

            if cycles_since_timing >= 30:
                cycles_since_timing = 0
                try:
                    ts = trade_log.timing_summary(min_trades=3)
                    if ts.get("by_hour_utc") or ts.get("by_window"):
                        logger.info(
                            f"TIMING_SUMMARY hour={ts.get('by_hour_utc')} window={ts.get('by_window')}"
                        )
                except Exception as e:
                    logger.debug(f"Timing summary failed: {e}")

            consecutive_errors = 0

        except Exception as e:
            consecutive_errors += 1
            logger.exception(f"Loop error ({consecutive_errors}): {e}")
            backoff = min(120, cycle * (2 ** min(consecutive_errors, 4)))
            if not _shutdown:
                time.sleep(backoff)
            continue

        if not _shutdown:
            time.sleep(cycle)

    _save_state(state, risk, learner, tracker)
    wr = (session_wins / session_resolves) if session_resolves else 0.0
    delta = risk.bankroll - session_start_bank
    logger.info(
        f"PROOF FINAL | session_pnl=${session_pnl:.2f} | bank_delta=${delta:.2f} | "
        f"bank=${risk.bankroll:.2f} | resolves={session_resolves} wr={wr:.1%}"
    )
    logger.info("Shutdown complete. State saved.")


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
