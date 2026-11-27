"""
ASTF — Adaptive Short-Term Filter
Self-learning multi-signal paper trader for Polymarket 5m/15m crypto markets.
"""
import argparse
import math
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
from src.learning.calibrated import CalibratedProbabilityModel
from src.execution.paper import PaperExecutor
from src.execution.positions import PositionTracker
from src.execution.exit_policy import evaluate_exit
from src.execution.paper_exit_lifecycle import PaperExitLifecycle
from src.utils.fees import taker_fee_usd, taker_fee_rate

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
    # Signal handlers must do as little as possible: Rich/console logging can
    # re-enter imports while Python is interrupted by SIGTERM.
    global _shutdown
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
        "fee_rate": float(feat.get("fee_rate", 0.07)),
        "fee_exponent": float(feat.get("fee_exponent", 1.0)),
        "fee_collection": feat.get("fee_collection", "usdc"),
        "fee_source": feat.get("fee_source", "config_fallback"),
        "vs_open": float(feat.get("vs_open") or 0),
    }


def _save_state(state: StateStore, risk: RiskManager, learner: AdaptiveLearner, tracker: PositionTracker):
    try:
        state.save({
            "bankroll": risk.bankroll,
            "peak_bankroll": risk.peak_bankroll,
            "daily_pnl": risk.daily_pnl,
            "risk_current_day": risk.current_day.isoformat(),
            "risk_paused": risk.paused,
            "risk_pause_reason": risk.pause_reason,
            "exploration_used_today": risk.exploration_used_today,
            "learner": learner.export_state(),
            "open_count": tracker.open_count(),
            "open_positions": tracker.export_open(),
            "paper_ledger_applied_total": float(getattr(risk, "_paper_ledger_applied_total", 0.0)),
            "paper_ledger_applied_daily": float(getattr(risk, "_paper_ledger_applied_daily", 0.0)),
            "paper_ledger_applied_day": str(getattr(risk, "_paper_ledger_applied_day", risk.current_day.isoformat())),
        })
    except Exception as e:
        logger.warning(f"State save failed: {e}")


def _mark_paper_ledger_applied(risk: RiskManager, ledger: PaperExitLifecycle):
    """Checkpoint ledger totals only after the matching risk PnL was applied."""
    summary = ledger.summary()
    risk._paper_ledger_applied_total = float(summary["net_realized_pnl_usd"])
    risk._paper_ledger_applied_daily = float(summary["daily_realized_net_pnl_usd"])
    risk._paper_ledger_applied_day = str(summary["daily_realized_day_utc"])


def _reconcile_paper_ledger_pnl(risk: RiskManager, ledger: PaperExitLifecycle) -> bool:
    """Recover realized PnL written to SQLite before a crash but not state JSON."""
    summary = ledger.summary()
    total = float(summary["net_realized_pnl_usd"])
    daily = float(summary["daily_realized_net_pnl_usd"])
    day = risk.current_day.isoformat()
    applied_total = float(getattr(risk, "_paper_ledger_applied_total", total))
    applied_day = str(getattr(risk, "_paper_ledger_applied_day", day))
    applied_daily = float(getattr(risk, "_paper_ledger_applied_daily", daily if applied_day == day else 0.0))
    if not all(math.isfinite(v) for v in (total, daily, applied_total, applied_daily)):
        risk.paused = True
        risk.pause_reason = "Non-finite paper PnL checkpoint; manual reconciliation required"
        return False
    if applied_total > total + 1e-6 or (applied_day == day and applied_daily > daily + 1e-6):
        risk.paused = True
        risk.pause_reason = "Paper PnL checkpoint exceeds durable ledger; manual reconciliation required"
        return False

    delta_total = total - applied_total
    delta_daily = daily - (applied_daily if applied_day == day else 0.0)
    if abs(delta_total) > 1e-9:
        risk.bankroll = max(0.0, risk.bankroll + delta_total)
        risk.peak_bankroll = max(risk.peak_bankroll, risk.bankroll)
    if abs(delta_daily) > 1e-9:
        risk.daily_pnl += delta_daily

    risk._paper_ledger_applied_total = total
    risk._paper_ledger_applied_daily = daily
    risk._paper_ledger_applied_day = day
    risk._check_circuit_breakers()
    return True


def _prune_cooldowns(cooldown_until: dict, now: float):
    dead = [k for k, t in cooldown_until.items() if t <= now]
    for k in dead:
        del cooldown_until[k]


def _validated_entry_quote(features: dict):
    """Return an executable ask only when it came from a validated two-sided book."""
    if not isinstance(features, dict) or not features.get("book_valid"):
        return None
    try:
        ask = float(features.get("ask_price"))
    except (TypeError, ValueError, OverflowError):
        return None
    return ask if math.isfinite(ask) and 0.0 < ask < 1.0 else None


def _can_open_for_market(
    risk, tracker, size: float, open_count: int, asset: str, window: str,
    fee_rate: float = 0.0, fee_exponent: float = 1.0,
    entry_price: float = 0.5, fee_collection: str = "usdc",
) -> bool:
    positions = tracker.get_open_list()
    reserved = sum(float(p.size_usd) for p in positions)
    asset_used = sum(float(p.size_usd) for p in positions if p.asset == asset)
    window_used = sum(float(p.size_usd) for p in positions if p.window == window)
    try:
        rate = float(fee_rate)
        exponent = float(fee_exponent)
        price = float(entry_price)
        collection = str(fee_collection).lower()
    except (TypeError, ValueError, OverflowError):
        return False
    if (not math.isfinite(rate) or not 0.0 <= rate <= 0.25
            or not math.isfinite(exponent) or not 0.0 < exponent <= 4.0
            or not math.isfinite(price) or not 0.0 < price < 1.0
            or collection not in ("usdc", "shares")):
        return False
    # USDC fees consume additional bankroll. Calculate their exact fraction
    # of entry notional at this price/exponent; the fee coefficient alone is
    # not a valid bound for fractional exponents below 1.
    fee_buffer_rate = (
        taker_fee_rate(price, rate=rate, exponent=exponent) / price
        if collection == "usdc" else 0.0
    )
    risk_notional = float(size) * (1.0 + fee_buffer_rate)
    return risk.can_open(
        risk_notional, open_count=open_count, reserved_exposure=reserved,
        asset_exposure=asset_used, window_exposure=window_used,
    )



def _sync_paper_ledger(tracker: PositionTracker, ledger: PaperExitLifecycle):
    """Make tracker state agree with the durable partial-fill event journal."""
    for pos in list(tracker.get_open_list()):
        durable = ledger.get_position(pos.order_id)
        metadata = {
            "asset": pos.asset, "window": pos.window, "hour_utc": pos.hour_utc,
            "weekday_utc": pos.weekday_utc, "time_remaining_s": pos.time_remaining_s,
            "time_remaining_pct": pos.time_remaining_pct, "score": pos.score,
            "edge_after_fees": pos.edge_after_fees, "momentum": pos.momentum,
            "vs_open": pos.vs_open, "regime": pos.regime, "trade_type": pos.trade_type,
            "fee_rate": pos.fee_rate, "fee_exponent": pos.fee_exponent,
            "fee_collection": pos.fee_collection,
            "slug": pos.slug, "end_date": pos.end_date,
        }
        if durable is None:
            ledger.record_entry_fill(
                position_id=pos.order_id, token_id=pos.token_id, market_id=pos.market_id,
                direction=pos.direction, quantity=pos.shares, price=pos.entry_price,
                fee_usd=pos.fee, fee_collection=pos.fee_collection,
                opened_at=pos.opened_at.isoformat(),
                metadata=metadata, event_id=f"entry:{pos.order_id}",
            )
            durable = ledger.get_position(pos.order_id)
            if durable is None:
                continue
        pos.shares = durable["remaining_quantity"]
        pos.entry_price = durable["average_entry_price"]
        # Entry fees are already reflected in the durable fee-adjusted basis;
        # do not charge them again.
        pos.fee = 0.0
        pos.size_usd = pos.shares * pos.entry_price
        if pos.shares <= 1e-9:
            tracker.open.pop(pos.order_id, None)

    for durable in ledger.open_positions():
        if durable["position_id"] in tracker.open:
            continue
        meta = durable["metadata"]
        opened = datetime.fromisoformat(durable["opened_at"].replace("Z", "+00:00"))
        remaining = durable["remaining_quantity"]
        avg = durable["average_entry_price"]
        entry_fee_remaining = max(0.0, durable["entry_fees_usd"] - durable["allocated_entry_fees_usd"])
        row = {
            "order_id": durable["position_id"], "market_id": durable["market_id"],
            "token_id": durable["token_id"], "direction": durable["direction"],
            "side": "buy", "entry_price": avg, "size_usd": remaining * avg,
            "shares": remaining, "fee": 0.0, "opened_at": opened.isoformat(),
            "slug": meta.get("slug", ""), "end_date": meta.get("end_date"),
            "fee_rate": meta.get("fee_rate", 0.07),
            "fee_exponent": meta.get("fee_exponent", 1.0),
            "fee_collection": meta.get("fee_collection", "usdc"),
            **{k: v for k, v in meta.items() if k not in ("slug", "end_date")},
        }
        tracker.import_open([row])


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

    reset_bankroll = os.getenv("PAPER_RESET_BANKROLL", "").lower() in ("1", "true", "yes")
    paper_lifecycle = PaperExitLifecycle(_data_dir() / "paper_lifecycle.db")
    ledger_summary = paper_lifecycle.summary()

    if reset_bankroll:
        start_bank = float(config.get("bankroll", 1000.0))
        logger.warning(f"PAPER_RESET_BANKROLL: starting fresh at ${start_bank:.2f}")
        saved = {}
    else:
        start_bank = float(saved.get("bankroll", config.get("bankroll", 1000.0)))

    risk = RiskManager(config, start_bank)
    # Restore intraday controls only for the same UTC date. Drawdown is
    # independently recalculated from the persisted peak bankroll.
    if saved.get("risk_current_day") == risk.current_day.isoformat():
        try:
            risk.daily_pnl = float(saved.get("daily_pnl", 0.0))
            risk.exploration_used_today = max(0.0, float(saved.get("exploration_used_today", 0.0)))
            if bool(saved.get("risk_paused")):
                risk.paused = True
                risk.pause_reason = str(saved.get("risk_pause_reason") or "Persisted risk pause")
            risk._check_circuit_breakers()
        except (TypeError, ValueError):
            logger.warning("Could not restore intraday risk state; keeping conservative defaults")
    if saved.get("peak_bankroll") and not reset_bankroll:
        try:
            risk.peak_bankroll = max(risk.bankroll, float(saved["peak_bankroll"]))
        except (TypeError, ValueError):
            pass

    today = risk.current_day.isoformat()
    if reset_bankroll:
        risk._paper_ledger_applied_total = float(ledger_summary["net_realized_pnl_usd"])
        risk._paper_ledger_applied_daily = float(ledger_summary["daily_realized_net_pnl_usd"])
        risk._paper_ledger_applied_day = today
    elif "paper_ledger_applied_total" in saved:
        risk._paper_ledger_applied_total = float(saved.get("paper_ledger_applied_total", 0.0))
        risk._paper_ledger_applied_daily = float(saved.get("paper_ledger_applied_daily", 0.0))
        risk._paper_ledger_applied_day = str(saved.get("paper_ledger_applied_day") or saved.get("risk_current_day") or today)
    else:
        # Backward-compatible migration: old state files have no ledger
        # checkpoint. Assume their realized PnL was already reflected, avoiding
        # a duplicate credit; all future commits are crash-recoverable.
        risk._paper_ledger_applied_total = float(ledger_summary["net_realized_pnl_usd"])
        risk._paper_ledger_applied_daily = (
            float(ledger_summary["daily_realized_net_pnl_usd"])
            if saved.get("risk_current_day") == today else 0.0
        )
        risk._paper_ledger_applied_day = str(saved.get("risk_current_day") or today)
        if not reset_bankroll:
            logger.warning("Migrating legacy state without a paper-ledger PnL checkpoint")

    if not _reconcile_paper_ledger_pnl(risk, paper_lifecycle):
        raise SystemExit(risk.pause_reason)

    learner = AdaptiveLearner(config)
    calibrator = CalibratedProbabilityModel(min_samples=15)
    learner.import_state(saved.get("learner") or {})
    if learner.get_min_score() > 0.70:
        logger.warning(
            f"Soft-capping restored min_score {learner.get_min_score():.3f} → 0.65"
        )
        learner.current_min_score = 0.65
        if learner.regime == "chaotic":
            learner.regime = "normal"

    fee_rate = float(config.get("signals", {}).get("fee_rate", 0.07))
    prefer_maker = bool(config.get("execution", {}).get("prefer_maker", True))
    paper_slippage = float(config.get("execution", {}).get("paper_slippage_bps", 20))
    executor = PaperExecutor(
        fee_rate=fee_rate, prefer_maker=prefer_maker, slippage_bps=paper_slippage,
        max_slippage_bps=float(config.get("execution", {}).get("max_slippage_bps", 30)),
    )
    tracker = PositionTracker()
    if saved.get("open_positions") and not reset_bankroll:
        try:
            tracker.import_open(saved["open_positions"])
        except Exception as e:
            logger.warning(f"Open position restore failed: {e}")

    trade_log = TradeLogger(path=_data_dir() / "trades.db")
    if reset_bankroll and paper_lifecycle.open_positions():
        raise SystemExit("Refusing bankroll reset while durable paper positions remain open; settle or close them first")
    _sync_paper_ledger(tracker, paper_lifecycle)

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
        f"degen={config.get('signals', {}).get('degen_mode', True)} | fee_sched~{fee_rate:.2f} | maker={prefer_maker}"
    )

    while not _shutdown:
        try:
            risk.update_bankroll(risk.bankroll)
            _reconcile_paper_ledger_pnl(risk, paper_lifecycle)

            if risk.paused:
                _save_state(state, risk, learner, tracker)
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
                            durable = paper_lifecycle.get_position(pos.order_id)
                            if durable and durable["is_open"]:
                                paper_lifecycle.record_settlement(
                                    position_id=pos.order_id,
                                    event_id=f"settle:{pos.order_id}",
                                    resolution_price=final,
                                )
                            trade = tracker.resolve(pos.order_id, final)
                            if trade:
                                risk.record_pnl(trade["pnl"])
                                _mark_paper_ledger_applied(risk, paper_lifecycle)
                                learner.log_trade({
                                    "score": trade.get("score", 0.7),
                                    "direction": trade["direction"],
                                    "entry_price": trade["entry_price"],
                                    "pnl": trade["pnl"],
                                    "size_usd": trade.get("size_usd", 0.0),
                                    "stale": False,
                                    "hour_utc": trade.get("hour_utc"),
                                    "time_remaining_s": trade.get("time_remaining_s"),
                                    "vs_open": trade.get("vs_open"),
                                    "momentum": trade.get("momentum"),
                                })
                                trade_log.log(trade, score=trade.get("score", 0))
                                _save_state(state, risk, learner, tracker)
                                session_resolves += 1
                                session_pnl += float(trade["pnl"])
                                if float(trade["pnl"]) > 0:
                                    session_wins += 1
                                calibrator.observe(trade)
                                logger.info(
                                    f"RESOLVED {pos.direction.upper()} → {final:.0f} | "
                                    f"pnl=${trade['pnl']:.3f} | "
                                    f"h={trade.get('hour_utc')} wd={trade.get('weekday_utc')} "
                                    f"win={trade.get('window')} t_left={trade.get('time_remaining_s', 0):.0f}s"
                                )
                            continue

                    # Paper-only TP/SL/time exits. Use a real bid and fresh
                    # timestamp; never use midpoint as executable sell evidence.
                    exit_cfg = config.get("exit_policy", {}) or {}
                    if bool(exit_cfg.get("enabled", True)):
                        try:
                            durable = paper_lifecycle.get_position(pos.order_id)
                            if durable and durable["is_open"]:
                                pos.shares = durable["remaining_quantity"]
                                pos.entry_price = durable["average_entry_price"]
                                # BUY fee shares are already reflected in the durable basis.
                                pos.fee = 0.0
                                pos.size_usd = pos.shares * pos.entry_price
                                book = data.get_order_book(pos.token_id)
                                bids = book.get("bids") or []
                                parsed_bids = []
                                for level in bids:
                                    try:
                                        if isinstance(level, dict):
                                            level_price, level_size = float(level.get("price") or 0), float(level.get("size") or 0)
                                        else:
                                            level_price, level_size = float(level[0]), float(level[1])
                                        if (math.isfinite(level_price) and math.isfinite(level_size)
                                                and 0 < level_price < 1 and level_size > 0):
                                            parsed_bids.append((level_price, level_size))
                                    except (TypeError, ValueError, IndexError):
                                        continue
                                bid, bid_size = max(parsed_bids, key=lambda x: x[0]) if parsed_bids else (0.0, 0.0)
                                fetched = float(book.get("fetched_at") or 0)
                                book_time = datetime.fromtimestamp(fetched, timezone.utc) if fetched > 0 else None
                                decision = evaluate_exit(
                                    enabled=True, entry_price=pos.entry_price, shares=pos.shares,
                                    opened_at=pos.opened_at, best_bid=bid, book_timestamp=book_time,
                                    take_profit_pct=exit_cfg.get("take_profit_pct", 0.20),
                                    stop_loss_pct=exit_cfg.get("stop_loss_pct", 0.15),
                                    max_hold_seconds=exit_cfg.get("max_hold_seconds", 180),
                                    max_book_age_seconds=exit_cfg.get("max_book_age_seconds", 3),
                                )
                                if decision.should_exit and bid_size > 0:
                                    fraction = min(1.0, max(0.01, float(exit_cfg.get("fill_fraction", 0.50))))
                                    qty = min(pos.shares * fraction, bid_size)
                                    slip_bps = max(0.0, float(exit_cfg.get("slippage_bps", paper_slippage)))
                                    exit_px = bid - bid * slip_bps / 10000.0
                                    if qty * exit_px >= 0.50 and 0 < exit_px < 1:
                                        exit_fee = taker_fee_usd(qty, exit_px, rate=pos.fee_rate,
                                                                  exponent=pos.fee_exponent)
                                        prior_exit_qty = durable["exit_quantity"]
                                        event_id = f"sell:{pos.order_id}:{prior_exit_qty + qty:.12f}"
                                        updated = paper_lifecycle.record_exit_fill(
                                            position_id=pos.order_id, event_id=event_id,
                                            quantity=qty, price=exit_px, fee_usd=exit_fee,
                                        )
                                        trade = tracker.partial_close(
                                            pos.order_id, qty, exit_px, exit_fee,
                                            decision.reason, event_id,
                                        )
                                        if trade:
                                            risk.record_pnl(trade["pnl"])
                                            _mark_paper_ledger_applied(risk, paper_lifecycle)
                                            learner.log_trade({
                                                "score": trade.get("score", 0.7),
                                                "direction": trade["direction"],
                                                "entry_price": trade["entry_price"],
                                                "pnl": trade["pnl"], "size_usd": trade["size_usd"],
                                                "stale": False, "hour_utc": trade.get("hour_utc"),
                                                "time_remaining_s": trade.get("time_remaining_s"),
                                                "vs_open": trade.get("vs_open"), "momentum": trade.get("momentum"),
                                            })
                                            trade_log.log(trade, score=trade.get("score", 0))
                                            _save_state(state, risk, learner, tracker)
                                            session_pnl += float(trade["pnl"])
                                            logger.info(
                                                "PAPER EXIT | %s | qty=%.5f @ %.4f | fee=$%.4f | "
                                                "gross=$%.4f net=$%.4f remaining=%.5f",
                                                decision.reason, qty, exit_px, exit_fee,
                                                trade["gross_pnl"], trade["pnl"], updated["remaining_quantity"],
                                            )
                                            if not updated["is_open"]:
                                                session_resolves += 1
                                                if float(trade["pnl"]) > 0:
                                                    session_wins += 1
                                            continue
                        except Exception as exit_error:
                            logger.warning("Paper exit evaluation failed for %s: %s", pos.order_id, exit_error)

                    stage = tracker.stale_stage(
                        pos,
                        grace_seconds=int(config.get("execution", {}).get("stale_grace_seconds", 300)),
                        max_hold_minutes=int(config.get("execution", {}).get("stale_max_hold_minutes", 35)),
                    )
                    if stage in ("soft", "hard"):
                        # A midpoint or arbitrary 0.50 mark is not an executable
                        # exit or authoritative settlement. Keep the position open.
                        logger.warning(
                            f"STALE {stage.upper()} | retaining unresolved position "
                            f"{pos.order_id}; awaiting authoritative market resolution"
                        )
                except Exception as e:
                    logger.warning(f"Resolve error for {getattr(pos, 'order_id', '?')}: {e}")

            try:
                win_cfg = config.get("markets", {}).get("windows") or ["5m"]
                markets = data.get_active_short_term_markets(assets=assets, windows=win_cfg)
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
                        # Allow one pyramid add in favorable regime; otherwise skip if already open
                        existing_open = [p for p in tracker.get_open_list() if (p.slug and p.slug == slug) or p.market_id == mid_id]
                        if existing_open and not (regime == "favorable" and len(existing_open) == 1):
                            continue

                        book_up = data.get_order_book(up_token)
                        book_down = data.get_order_book(down_token)
                        max_book_age = float(config.get("execution", {}).get("max_book_age_seconds", 8))
                        if any(time.time() - float(b.get("fetched_at", 0)) > max_book_age
                               for b in (book_up, book_down)):
                            continue
                        up_mid = book_up.get("mid")
                        down_mid = book_down.get("mid")
                        up_ask = book_up.get("best_ask") or best_ask(book_up)
                        down_ask = book_down.get("best_ask") or best_ask(book_down)

                        # A midpoint is a valuation reference, not a fill.
                        # Directional BUY entries require an actual ask on that token.
                        if up_ask is None and down_ask is None:
                            continue

                        hist = list(price_history.get(asset, []))
                        mtf = prices.multi_timeframe_momentum(asset)
                        mom = prices.momentum(asset, hist)
                        # Short-window lag: sharp spot move while book mid is still near 0.5
                        if len(hist) >= 4 and hist[-4] > 0 and hist[-1] > 0:
                            move = (hist[-1] - hist[-4]) / hist[-4]
                            if abs(move) > 0.0008:
                                mid_now = float(up_mid or 0.5)
                                if 0.42 < mid_now < 0.58:
                                    lag = 0.15 if move > 0 else -0.15
                                    mom = max(-1.0, min(1.0, mom + lag))

                        try:
                            live_px = float(prices.get_fresh_price(asset, max_age=15.0) or 0)
                        except Exception:
                            live_px = 0.0
                        if live_px <= 0:
                            logger.debug(f"Skipping {slug}: spot price feed is stale")
                            continue
                        if slug and live_px > 0 and slug not in window_open_px:
                            rem = float(m.get("_remaining_s") or 0)
                            win_s = float(m.get("_window_seconds") or 300)
                            # Accept an observation in the first 15 seconds of
                            # the window; a mid-window restart must not relabel
                            # a late price as the market's opening reference.
                            if win_s - 15 <= rem <= win_s + 5:
                                window_open_px[slug] = live_px
                        open_px = window_open_px.get(slug)
                        vs_open = 0.0
                        if open_px and open_px > 0 and live_px > 0:
                            vs_open = max(-1.0, min(1.0, (live_px - open_px) / open_px * 150))
                        # Settlement proxy: 60s average vs open
                        avg60 = prices.get_60s_avg(asset)
                        if avg60 and open_px and open_px > 0:
                            vs_open = max(-1.0, min(1.0, (avg60 - open_px) / open_px * 150))

                        # Cross-asset lag
                        other_asset = "eth" if asset == "btc" else "btc"
                        other_vs = 0.0
                        other_slugs = [k for k in window_open_px if other_asset in k.lower()]
                        if other_slugs and live_px > 0:
                            o_open = window_open_px.get(other_slugs[-1])
                            if o_open and o_open > 0:
                                try:
                                    o_live = float(prices.get_fresh_price(other_asset, max_age=15.0) or 0)
                                    if o_live > 0:
                                        other_vs = max(-1.0, min(1.0, (o_live - o_open) / o_open * 150))
                                except Exception:
                                    pass

                        feat_up = build_features(
                            m, book_up, external_momentum=mom, side="up",
                            book_other=book_down, vs_open=vs_open,
                        )
                        feat_up["other_vs_open"] = other_vs

                        # Cross-window lead-lag: real 5m mid for 15m markets
                        window_lead = 0.0
                        if "15m" in (slug or "").lower() or m.get("_window") == "15m":
                            for om in markets:
                                if om.get("_asset") == asset and "5m" in (om.get("slug") or "").lower():
                                    try:
                                        o_up_token = om.get("_up_token")
                                        if o_up_token:
                                            o_book = data.get_order_book(o_up_token)
                                            o_up = o_book.get("mid") or 0.5
                                            if o_up > 0.62:
                                                window_lead = min(1.0, (o_up - 0.5) * 2.4)
                                            elif o_up < 0.38:
                                                window_lead = max(-1.0, (o_up - 0.5) * 2.4)
                                    except Exception:
                                        pass
                                    break
                        feat_up["window_lead"] = window_lead
                        feat_dn = build_features(
                            m, book_down, external_momentum=mom, side="down",
                            book_other=book_up, vs_open=vs_open,
                        )
                        feat_dn["other_vs_open"] = other_vs
                        feat_dn["window_lead"] = window_lead

                        # Complete-set opportunities are monitored but not executed.
                        # Two sequential legs are not atomic; a one-leg fill creates
                        # unhedged directional exposure and can fake arbitrage profits.
                        arb_watch = ensemble.complete_set_arb(
                            up_mid, down_mid, up_ask=up_ask, down_ask=down_ask,
                            fee_rate=feat_up.get("fee_rate", fee_rate),
                            fee_exponent=feat_up.get("fee_exponent", 1.0),
                        )
                        if arb_watch and config.get("execution", {}).get("execute_complete_set", False):
                            edge = float(arb_watch.get("edge") or 0)
                            min_edge = float(config.get("execution", {}).get("complete_set_min_edge", 0.002))
                            max_pair = float(config.get("execution", {}).get("complete_set_max_pair", 0.97))
                            if edge > min_edge:
                                try:
                                    up_px = float(arb_watch["up_price"])
                                    dn_px = float(arb_watch["down_price"])
                                except (TypeError, ValueError, KeyError):
                                    continue
                                if not (0.01 < up_px < 0.99 and 0.01 < dn_px < 0.99):
                                    continue
                                pair = up_px + dn_px
                                if 0.88 < pair < max_pair:
                                    # Scales with bankroll; soft cap at 5% so it compounds
                                    budget = min(float(risk.bankroll) * 0.04, float(risk.bankroll) * 0.05)
                                    n = budget / pair if pair > 0 else 0
                                    su, sd = n * up_px, n * dn_px
                                    if su > 0.5 and sd > 0.5 and risk.can_open(su + sd, open_count=open_n):
                                        meta = _timing_meta(m, feat_up, 0.95, edge, mom, regime, "arb")
                                        ok = 0
                                        for side, tok, px, sz in [("up", up_token, up_px, su), ("down", down_token, dn_px, sd)]:
                                            ord_ = executor.place_order(tok, "buy", px, sz, mid_id, already_aggressive=True)
                                            if ord_:
                                                tracker.add(ord_, direction=side, slug=slug, end_date=m.get("endDate") or m.get("end_date_iso"), meta=meta)
                                                open_n += 1
                                                ok += 1
                                        if ok == 2:
                                            cooldown_until[mid_id] = now + cooldown_sec
                                            trades_this_cycle += 1
                                            logger.info(f"ARB EXECUTE | edge={edge:.3f} | spend=${su+sd:.1f} | {slug[:32]}")
                                            continue
                        if arb_watch:
                            logger.info(f"ARB WATCH | edge={arb_watch.get('edge',0):.3f} | {slug[:40]}")

                        # Maker-only two-sided grid (primary path). Resting bids, hold to settle.
                        # Models the profitable high-volume maker wallets: zero fee, occasional cheap or late fills.
                        if config.get("execution", {}).get("maker_only", True):
                            clip = min(float(risk.bankroll) * 0.007, 3.5)
                            if clip >= 0.6:
                                # Dynamic levels: cheap relative bids + high-side only when mid is already elevated
                                # Inventory skew: reduce size on the side we already hold more of
                                held = {"up": 0.0, "down": 0.0}
                                for o in tracker.open_positions.values():
                                    if o.get("market_id") == mid_id:
                                        held[o.get("direction", "up")] = held.get(o.get("direction", "up"), 0) + float(o.get("size_usd") or 0)
                                for side, tok, mid, feat in [("up", up_token, up_mid, feat_up), ("down", down_token, down_mid, feat_dn)]:
                                    mom_s = float(feat.get("momentum") or 0)
                                    # Skip quoting a side that is strongly against the short-term move
                                    if (side == "up" and mom_s < -0.012) or (side == "down" and mom_s > 0.012):
                                        continue
                                    skew = 1.0 if held[side] < held.get("up" if side == "down" else "down", 0) + 2 else 0.55
                                    # Cheap relative levels
                                    levels = [round(max(0.04, mid - off), 3) for off in (0.04, 0.08, 0.14)]
                                    # High-side resting only when mid is already high (late exit style)
                                    if mid > 0.72:
                                        levels += [round(min(0.96, mid - 0.03), 3), round(min(0.96, mid + 0.02), 3)]
                                    for lvl in levels:
                                        if not (0.03 < lvl < 0.97) or abs(mid - lvl) > 0.28:
                                            continue
                                        ord_ = executor.place_maker_bid(tok, lvl, clip * skew, mid_id, mid)
                                        if ord_:
                                            meta = _timing_meta(m, feat, 0.5, 0.0, mom, regime, "maker")
                                            tracker.add(ord_, direction=side, slug=slug, end_date=m.get("endDate") or m.get("end_date_iso"), meta=meta)
                                            open_n += 1
                                            trades_this_cycle += 1
                                            logger.info(f"MAKER GRID {side.upper()} | bid={lvl:.2f} | mid={mid:.2f} | skew={skew:.1f} | {slug[:28]}")
                            # Skip taker paths when maker_only is on
                            continue

                        # Late-window winner: only when one side is already priced as the favorite
                        # and spot has confirmed the move. Skip pure directional otherwise.
                        remaining = float(feat_up.get("time_remaining_s") or 999)
                        if 15 < remaining < 100:
                            for side, feat, tok, book in [("up", feat_up, up_token, book_up), ("down", feat_dn, down_token, book_down)]:
                                lw = ensemble.late_winner(feat, side)
                                if lw.get("pass"):
                                    entry = float(lw.get("entry") or 0.7)
                                    size = min(float(risk.bankroll) * 0.03, 12.0)
                                    if size > 1.0 and risk.can_open(size, open_count=open_n):
                                        meta = _timing_meta(m, feat, lw["score"], lw["edge"], mom, regime, "late")
                                        ord_ = executor.place_order(tok, "buy", entry, size, mid_id, already_aggressive=True)
                                        if ord_:
                                            tracker.add(ord_, direction=side, slug=slug, end_date=m.get("endDate") or m.get("end_date_iso"), meta=meta)
                                            open_n += 1
                                            cooldown_until[mid_id] = now + cooldown_sec
                                            trades_this_cycle += 1
                                            logger.info(f"LATE WINNER {side.upper()} | score={lw['score']:.3f} | entry={entry:.3f} | t_left={remaining:.0f}s | {slug[:32]}")
                                            break

                        # Directional only as fallback; the 174-resolve sample showed negative expectancy.
                        # Keep the call for logging but require a much higher bar.
                        cal_prob_up = calibrator.calibrate(0.52, datetime.now(timezone.utc).hour, float(feat_up.get("time_remaining_s") or 300), vs_open)
                        sig_up = ensemble.directional_score(feat_up, preferred_side="up", calibrated_prob=cal_prob_up)
                        cal_prob_dn = calibrator.calibrate(0.52, datetime.now(timezone.utc).hour, float(feat_dn.get("time_remaining_s") or 300), vs_open)
                        sig_dn = ensemble.directional_score(feat_dn, preferred_side="down", calibrated_prob=cal_prob_dn)

                        candidates = []
                        for sig, feat, token in [
                            (sig_up, feat_up, up_token),
                            (sig_dn, feat_dn, down_token),
                        ]:
                            # Do not execute from the raw book quote. The
                            # feature builder must first validate a finite,
                            # two-sided, uncrossed book and derive its ask.
                            entry_f = _validated_entry_quote(feat)
                            if entry_f is None:
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

                        # Explicitly enabled uncalibrated entries are tiny
                        # discovery probes, never alpha-qualified trades.
                        is_expl = bool(sig.get("paper_probe")) or not sig.get("pass")
                        direction = sig["direction"]
                        edge = sig.get("edge_after_fees")

                        # Setup-bucket boost
                        boost = 0.0
                        try:
                            boost = learner.get_bucket_boost(
                                datetime.now(timezone.utc).hour,
                                float(feat.get("time_remaining_s") or 0),
                                vs_open,
                            )
                        except Exception:
                            boost = 0.0
                        if boost:
                            sig["score"] = max(0.0, min(1.0, sig.get("score", 0.5) + boost))
                            if edge is not None:
                                edge = edge + boost * 0.5

                        size = risk.size_position(
                            confidence=sig["score"],
                            est_prob=sig.get("est_prob"),
                            price=entry,
                            is_exploration=is_expl,
                            edge_after_fees=edge,
                            regime=regime,
                        )
                        if size <= 0 or not _can_open_for_market(risk, tracker, size, open_n, asset, str(m.get("_window") or ""), fee_rate=float(feat.get("fee_rate", fee_rate)), fee_exponent=float(feat.get("fee_exponent", 1.0)), entry_price=entry, fee_collection=feat.get("fee_collection", "usdc")):
                            continue

                        # Regime-conditional pyramid (once per market)
                        pyr_key = mid_id + "|pyr"
                        existing = [p for p in tracker.get_open_list() if p.slug == slug]
                        if (regime == "favorable" and existing and len(existing) == 1
                            and cooldown_until.get(pyr_key, 0) <= now):
                            size = size * 0.50
                            if not _can_open_for_market(risk, tracker, size, open_n, asset, str(m.get("_window") or ""), fee_rate=float(feat.get("fee_rate", fee_rate)), fee_exponent=float(feat.get("fee_exponent", 1.0)), entry_price=entry, fee_collection=feat.get("fee_collection", "usdc")):
                                continue
                            cooldown_until[pyr_key] = now + cooldown_sec * 2

                        trade_type = "probe" if sig.get("paper_probe") else ("expl" if is_expl else "dir")
                        meta = _timing_meta(
                            m, feat, sig["score"], edge or 0, mom, regime, trade_type
                        )
                        selected_book = book_up if token == up_token else book_down
                        order = executor.place_order(
                            token, "buy", entry, size, mid_id, already_aggressive=True,
                            fee_rate=float(feat.get("fee_rate", fee_rate)),
                            fee_exponent=float(feat.get("fee_exponent", 1.0)),
                            fee_collection=feat.get("fee_collection", "usdc"),
                            book_asks=selected_book.get("asks") or [],
                        )
                        if not order:
                            continue
                        entry_opened_at = datetime.now(timezone.utc)
                        paper_lifecycle.record_entry_fill(
                            position_id=order["order_id"], token_id=token,
                            market_id=mid_id, direction=direction, quantity=order["shares"],
                            price=order["fill_price"], fee_usd=order["fee"],
                            fee_shares=order.get("fee_shares", 0.0),
                            fee_collection=order.get("fee_collection", "usdc"),
                            opened_at=entry_opened_at.isoformat(),
                            metadata={**meta, "slug": slug, "end_date": m.get("endDate") or m.get("end_date_iso")},
                            event_id=f"entry:{order['order_id']}",
                        )
                        tracker.add(
                            order, direction=direction, slug=slug,
                            end_date=m.get("endDate") or m.get("end_date_iso"),
                            meta=meta, opened_at=entry_opened_at,
                        )
                        _save_state(state, risk, learner, tracker)
                        open_n += 1
                        trades_this_cycle += 1
                        cooldown_until[mid_id] = now + cooldown_sec
                        if is_expl:
                            risk.record_exploration(size)
                        _save_state(state, risk, learner, tracker)

                        tag = "EXPL" if is_expl else "DIR"
                        logger.info(
                            f"{tag} {direction.upper()} | score={sig['score']:.3f} | "
                            f"entry={entry:.3f} | edge={edge if edge is not None else 0:.3f} | "
                            f"mom={mom:.2f} vs_open={vs_open:.2f} other={other_vs:.2f} lead={window_lead:.2f} "
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
                paper_metrics = paper_lifecycle.summary()
                logger.info(
                    "PAPER LIFECYCLE | entry_shares=%.4f exit_shares=%.4f open=%d "
                    "gross_realized=$%.4f net_realized=$%.4f modeled_fees=$%.4f",
                    paper_metrics["entry_shares"], paper_metrics["exit_shares"],
                    paper_metrics["open_positions"], paper_metrics["gross_realized_pnl_usd"],
                    paper_metrics["net_realized_pnl_usd"], paper_metrics["fees_usd"],
                )
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

    logger.info("Shutdown signal received; finishing cycle and persisting state.")
    _save_state(state, risk, learner, tracker)
    paper_metrics = paper_lifecycle.summary()
    logger.info(
        "PAPER LIFECYCLE FINAL | entry_shares=%.4f exit_shares=%.4f open=%d "
        "gross_realized=$%.4f net_realized=$%.4f modeled_fees=$%.4f",
        paper_metrics["entry_shares"], paper_metrics["exit_shares"],
        paper_metrics["open_positions"], paper_metrics["gross_realized_pnl_usd"],
        paper_metrics["net_realized_pnl_usd"], paper_metrics["fees_usd"],
    )
    wr = (session_wins / session_resolves) if session_resolves else 0.0
    delta = risk.bankroll - session_start_bank
    logger.info(
        f"PROOF FINAL | session_pnl=${session_pnl:.2f} | bank_delta=${delta:.2f} | "
        f"bank=${risk.bankroll:.2f} | resolves={session_resolves} wr={wr:.1%}"
    )
    logger.info("Shutdown complete. State saved.")


def run_live(config: dict):
    # The live adapter is present for isolated, mocked testing only. It is
    # intentionally not wired into this strategy loop: paper remains the only
    # supported runtime until live fill reconciliation, exits, and restart
    # recovery are implemented and separately approved.
    logger.error(
        "LIVE MODE REFUSED: dormant live adapter is not connected to the strategy loop. "
        "Run --mode paper; no real orders were sent."
    )
    raise SystemExit(2)


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
