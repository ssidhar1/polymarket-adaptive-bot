import time
from datetime import datetime, timedelta, timezone

import pytest

from src.execution.paper_exit_lifecycle import PaperExitLifecycle
from src.risk.manager import RiskManager

from src.data.price_feed import PriceFeed
from src import main
from src.main import _reconcile_paper_ledger_pnl


def test_sigterm_handler_only_sets_flag(monkeypatch):
    monkeypatch.setattr(main, "_shutdown", False)
    def forbidden_log(*args, **kwargs):
        raise AssertionError("signal handler must not render/log")
    monkeypatch.setattr(main.logger, "info", forbidden_log)
    main._handle_signal(15, None)
    assert main._shutdown is True


def test_price_feed_never_returns_cache_older_than_freshness_limit():
    feed = PriceFeed()
    feed.cache = {"btc": 100.0}
    feed.cache_updated_at = {"btc": time.time() - 16.0}
    feed.last_fetch = time.time()
    assert feed.get_prices() == {}
    assert feed.get_fresh_price("btc", max_age=15.0) is None


def test_crash_recovery_applies_uncheckpointed_realized_pnl_once(tmp_path):
    ledger = PaperExitLifecycle(tmp_path / "paper.db")
    ledger.record_entry_fill(
        position_id="p", token_id="t", market_id="m", direction="up",
        quantity=10, price=0.40, fee_usd=0.04, event_id="buy",
    )
    # Simulate a crash after SQLite committed the sell, but before JSON state
    # saved the corresponding bankroll update.
    ledger.record_exit_fill(
        position_id="p", event_id="sell", quantity=10, price=0.50, fee_usd=0.05,
    )
    risk = RiskManager({
        "risk": {
            "max_position_pct": 0.04, "max_concurrent": 6,
            "max_total_exposure_pct": 0.20, "max_asset_exposure_pct": 0.14,
            "max_window_exposure_pct": 0.18, "daily_loss_limit_pct": 0.05,
            "max_drawdown_pct": 0.12, "probability_model_calibrated": False,
        },
        "learning": {"exploration_pct": 0.02},
    }, 1000.0)
    risk._paper_ledger_applied_total = 0.0
    risk._paper_ledger_applied_daily = 0.0
    risk._paper_ledger_applied_day = risk.current_day.isoformat()

    assert _reconcile_paper_ledger_pnl(risk, ledger) is True
    assert risk.bankroll == pytest.approx(1000.91)
    assert risk.daily_pnl == pytest.approx(0.91)
    # A second reconciliation must not double-credit the same durable event.
    assert _reconcile_paper_ledger_pnl(risk, ledger) is True
    assert risk.bankroll == pytest.approx(1000.91)
    assert risk.daily_pnl == pytest.approx(0.91)


def test_pnl_checkpoint_ahead_of_ledger_pauses_trading(tmp_path):
    ledger = PaperExitLifecycle(tmp_path / "paper.db")
    risk = RiskManager({"risk": {"max_drawdown_pct": 0.12}}, 1000.0)
    risk._paper_ledger_applied_total = 1.0
    risk._paper_ledger_applied_daily = 0.0
    risk._paper_ledger_applied_day = risk.current_day.isoformat()
    assert _reconcile_paper_ledger_pnl(risk, ledger) is False
    assert risk.paused is True
    assert "checkpoint exceeds durable ledger" in risk.pause_reason



@pytest.mark.parametrize("features", [
    {},
    {"book_valid": False, "ask_price": 0.50},
    {"book_valid": True, "ask_price": None},
    {"book_valid": True, "ask_price": float("nan")},
    {"book_valid": True, "ask_price": float("inf")},
    {"book_valid": True, "ask_price": 0.0},
    {"book_valid": True, "ask_price": 1.0},
])
def test_paper_entry_quote_requires_validated_finite_book(features):
    assert main._validated_entry_quote(features) is None


def test_paper_entry_quote_uses_only_validated_best_ask():
    assert main._validated_entry_quote({
        "book_valid": True, "ask_price": 0.43,
    }) == pytest.approx(0.43)
