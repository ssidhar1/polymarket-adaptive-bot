from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.execution.live import LiveExecutionError, LiveExecutor, PER_ORDER_APPROVAL
from src.execution.live_lifecycle import LiveLifecycle

ASSET = "12345678901234567890"


class Page:
    def __init__(self, items=()):
        self.items = list(items)

    def iter_items(self):
        return iter(self.items)


class FakeClient:
    def __init__(self):
        self.orders = []
        self.trades = []
        self.positions = []
        self.cancelled = []
        self.posts = []
        self.book = SimpleNamespace(
            timestamp=datetime.now(timezone.utc),
            bids=[SimpleNamespace(price=Decimal(".49"), size=Decimal("100"))],
            asks=[SimpleNamespace(price=Decimal(".51"), size=Decimal("100"))],
            min_order_size=Decimal("1"),
        )

    def list_open_orders(self):
        return Page(self.orders)

    def list_account_trades(self, asset_id=None):
        return Page([t for t in self.trades if t.asset_id == asset_id])

    def list_positions(self, status=None, full_history=False):
        assert status == "OPEN"
        return Page(self.positions)

    def get_order_book(self, asset_id):
        return self.book

    def get_balance_allowance(self, asset_type, asset_id=None):
        return SimpleNamespace(balance=2_000_000_000, allowances={"exchange": 2_000_000_000})

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return SimpleNamespace(canceled=(order_id,), not_canceled={})

    def create_limit_order(self, **kwargs):
        self.posts.append(("signed", kwargs))
        return object()

    def post_order(self, signed):
        return SimpleNamespace(ok=True, status="live", order_id="exit-oid", trade_ids=())


def config():
    return {"live_execution": {
        "enabled": True, "max_order_notional_usd": 10, "max_position_pct": .01,
        "max_total_exposure_pct": .05, "max_open_orders": 1,
        "max_book_age_seconds": 3, "max_spread": .05, "order_ttl_seconds": 600,
    }}


def env():
    return {
        "ASTF_ENABLE_LIVE_TRADING": "YES",
        "ASTF_LIVE_CONFIRMATION": "I_ACCEPT_REAL_MONEY_RISK",
        "DEFAULT_MODE": "live",
    }


def setup(tmp_path, client=None):
    client = client or FakeClient()
    executor = LiveExecutor(client, config(), tmp_path / "journal.sqlite", environ=env())
    lifecycle = LiveLifecycle(executor, backoff_seconds=0, sleep=lambda _: None)
    return client, executor, lifecycle


def seed_order(executor, *, status="ACCEPTED", order_id="oid-1", side="BUY"):
    executor._reserve("intent-0001", ASSET, Decimal(".50"), Decimal("10"))
    with executor._db() as db:
        db.execute("UPDATE live_order_intents SET side=? WHERE intent_key='intent-0001'", (side,))
    executor._update("intent-0001", status, order_id)


def exchange_order(*, expiry=None, matched="4", side="BUY", order_id="oid-1"):
    return SimpleNamespace(
        id=order_id, asset_id=ASSET, side=side, price=Decimal(".50"),
        original_size=Decimal("10"), size_matched=Decimal(matched),
        expires_at=expiry or (datetime.now(timezone.utc) + timedelta(minutes=5)),
        status="LIVE",
    )


def exchange_position(shares="4"):
    return SimpleNamespace(
        asset_id=ASSET, condition_id="0xcondition", current_size=Decimal(shares),
        avg_price=Decimal(".50"), total_cost_usdc=Decimal("2.00"),
        current_value=Decimal("2.04"), unrealized_pnl=Decimal(".04"),
        realized_pnl=Decimal("0"), status="OPEN",
    )


def exchange_trade(trade_id="trade-1", size="4"):
    return SimpleNamespace(
        id=trade_id, asset_id=ASSET, taker_order_id="oid-1", side="BUY",
        price=Decimal(".50"), size=Decimal(size), fee_rate_bps=Decimal("700"),
        matched_at=datetime.now(timezone.utc), maker_orders=(),
    )


def test_partial_fills_are_deduplicated_across_restart(tmp_path):
    client, executor, lifecycle = setup(tmp_path)
    seed_order(executor)
    client.orders = [exchange_order()]
    client.trades = [exchange_trade()]
    client.positions = [exchange_position()]

    first = lifecycle.reconcile()
    assert first["new_fills"] == 1
    with executor._db() as db:
        assert db.execute("SELECT COUNT(*) FROM live_fills").fetchone()[0] == 1
        assert db.execute("SELECT filled_size FROM live_order_intents").fetchone()[0] == "4"

    # A new process/manager sees the same exchange history but must not book it twice.
    restarted = LiveLifecycle(executor, backoff_seconds=0, sleep=lambda _: None)
    second = restarted.reconcile()
    assert second["new_fills"] == 0
    assert restarted.positions[ASSET]["shares"] == Decimal("4")


def test_lost_submit_ack_is_recovered_only_on_exact_fingerprint(tmp_path):
    client, executor, lifecycle = setup(tmp_path)
    seed_order(executor, status="UNKNOWN", order_id=None)
    client.orders = [exchange_order()]
    lifecycle.reconcile()
    with executor._db() as db:
        row = db.execute("SELECT order_id,status FROM live_order_intents").fetchone()
    assert row == ("oid-1", "PARTIALLY_FILLED")


def test_ambiguous_multiple_order_recovery_is_not_guessed(tmp_path):
    client, executor, lifecycle = setup(tmp_path)
    seed_order(executor, status="UNKNOWN", order_id=None)
    client.orders = [exchange_order(order_id="oid-1"), exchange_order(order_id="oid-2")]
    lifecycle.reconcile()
    with executor._db() as db:
        row = db.execute("SELECT order_id,status FROM live_order_intents").fetchone()
    assert row[0] is None
    assert row[1] == "NOT_OPEN_RECONCILE_REQUIRED"


def test_expiry_manager_requests_cancel_and_waits_for_verification(tmp_path):
    client, executor, lifecycle = setup(tmp_path)
    seed_order(executor)
    client.orders = [exchange_order(expiry=datetime.now(timezone.utc) - timedelta(seconds=2), matched="2")]
    lifecycle.reconcile()
    assert lifecycle.cancel_expired_orders(safety_seconds=5) == ["oid-1"]
    assert client.cancelled == ["oid-1"]
    with executor._db() as db:
        assert db.execute("SELECT status FROM live_order_intents").fetchone()[0] == "CANCEL_REQUESTED"


def test_cancel_ambiguous_outcome_is_not_retried_automatically(tmp_path):
    client, executor, lifecycle = setup(tmp_path)
    seed_order(executor)
    client.orders = [exchange_order(expiry=datetime.now(timezone.utc) - timedelta(seconds=2))]
    lifecycle.reconcile()
    def timeout(order_id):
        raise TimeoutError("unknown cancel outcome")
    client.cancel_order = timeout
    assert lifecycle.cancel_expired_orders() == []
    with executor._db() as db:
        assert db.execute("SELECT status FROM live_order_intents").fetchone()[0] == "CANCEL_UNKNOWN"


def test_exit_requires_reconciled_position_and_is_passive_sell(tmp_path):
    client, executor, lifecycle = setup(tmp_path)
    client.positions = [exchange_position()]
    lifecycle.reconcile()
    result = lifecycle.submit_exit_limit(
        intent_key="exit-intent-001", asset_id=ASSET, price=".52",
        shares="2", approval=PER_ORDER_APPROVAL,
    )
    assert result["ok"] is True
    assert client.posts[0][1]["side"] == "SELL"
    assert client.posts[0][1]["post_only"] is True


def test_exit_rejects_size_above_wallet_position(tmp_path):
    client, executor, lifecycle = setup(tmp_path)
    client.positions = [exchange_position()]
    lifecycle.reconcile()
    with pytest.raises(LiveExecutionError, match="exceeds held shares"):
        lifecycle.submit_exit_limit(
            intent_key="exit-intent-002", asset_id=ASSET, price=".52",
            shares="5", approval=PER_ORDER_APPROVAL,
        )
    assert client.posts == []


def test_exit_rejects_crossing_sell_price(tmp_path):
    client, executor, lifecycle = setup(tmp_path)
    client.positions = [exchange_position()]
    lifecycle.reconcile()
    with pytest.raises(LiveExecutionError, match="cross/lock"):
        lifecycle.submit_exit_limit(
            intent_key="exit-intent-003", asset_id=ASSET, price=".48",
            approval=PER_ORDER_APPROVAL,
        )
    assert client.posts == []


def test_rate_limit_retries_read_only_calls_with_bounded_backoff(tmp_path):
    _, _, lifecycle = setup(tmp_path)
    class RateLimitError(Exception):
        pass
    calls = {"n": 0}
    def read():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RateLimitError("slow down")
        return "ok"
    assert lifecycle._read(read) == "ok"
    assert calls["n"] == 3


def test_unknown_read_error_is_not_retried(tmp_path):
    _, _, lifecycle = setup(tmp_path)
    calls = {"n": 0}
    def read():
        calls["n"] += 1
        raise ValueError("bad response")
    with pytest.raises(ValueError):
        lifecycle._read(read)
    assert calls["n"] == 1

def test_lost_ack_filled_order_recovered_from_unique_account_trade(tmp_path):
    client, executor, lifecycle = setup(tmp_path)
    seed_order(executor, status="UNKNOWN", order_id=None)
    client.orders = []
    client.trades = [exchange_trade(size="10")]
    client.positions = [exchange_position(shares="10")]
    lifecycle.reconcile()
    with executor._db() as db:
        row = db.execute("SELECT order_id,filled_size FROM live_order_intents").fetchone()
    assert row == ("oid-1", "10")
    with executor._db() as db:
        assert db.execute("SELECT COUNT(*) FROM live_fills").fetchone()[0] == 1

def test_acknowledged_cancel_is_terminal_only_after_absent_from_open_orders(tmp_path):
    client, executor, lifecycle = setup(tmp_path)
    seed_order(executor)
    client.orders = [exchange_order(expiry=datetime.now(timezone.utc) - timedelta(seconds=2), matched="2")]
    lifecycle.reconcile()
    lifecycle.cancel_expired_orders()
    client.orders = []
    lifecycle.reconcile()
    with executor._db() as db:
        assert db.execute("SELECT status FROM live_order_intents").fetchone()[0] == "CANCELED"


def test_gtd_order_is_marked_expired_only_after_expiry_and_absence(tmp_path):
    client, executor, lifecycle = setup(tmp_path)
    seed_order(executor)
    with executor._db() as db:
        db.execute("UPDATE live_order_intents SET expires_at=? WHERE intent_key='intent-0001'",
                   ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))
    client.orders = []
    lifecycle.reconcile()
    with executor._db() as db:
        assert db.execute("SELECT status FROM live_order_intents").fetchone()[0] == "EXPIRED"


def test_paginated_read_restarts_from_first_page_after_rate_limit(tmp_path):
    _, _, lifecycle = setup(tmp_path)
    class RateLimitError(Exception):
        pass
    calls = {"n": 0}
    class FlakyPage:
        def iter_items(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RateLimitError("rate limited during page fetch")
            return iter(["complete"])
    def factory():
        return FlakyPage()
    assert lifecycle._read_items(factory) == ["complete"]
    assert calls["n"] == 2

def test_exit_fill_is_reconciled_against_wallet_position(tmp_path):
    client, executor, lifecycle = setup(tmp_path)
    client.positions = [exchange_position()]
    lifecycle.reconcile()
    result = lifecycle.submit_exit_limit(
        intent_key="exit-intent-004", asset_id=ASSET, price=".52",
        shares="2", approval=PER_ORDER_APPROVAL,
    )
    assert result["ok"]
    client.orders = [exchange_order(order_id="exit-oid", side="SELL", matched="2")]
    client.trades = [SimpleNamespace(
        id="trade-sell-1", asset_id=ASSET, taker_order_id="exit-oid", side="SELL",
        price=Decimal(".52"), size=Decimal("2"), fee_rate_bps=Decimal("700"),
        matched_at=datetime.now(timezone.utc), maker_orders=(),
    )]
    client.positions = [exchange_position(shares="2")]
    lifecycle.reconcile()
    with executor._db() as db:
        fill = db.execute("SELECT side,size FROM live_fills WHERE trade_id='trade-sell-1'").fetchone()
    assert fill == ("SELL", "2")
    assert lifecycle.positions[ASSET]["shares"] == Decimal("2")


def test_live_entrypoint_remains_hard_disabled(tmp_path):
    from src.main import run_live
    with pytest.raises(SystemExit) as exc:
        run_live(config())
    assert exc.value.code == 2

def test_delayed_trade_indexing_does_not_double_count_matched_size(tmp_path):
    client, executor, lifecycle = setup(tmp_path)
    seed_order(executor)
    client.orders = [exchange_order(matched="4")]
    client.trades = []
    lifecycle.reconcile()
    with executor._db() as db:
        assert db.execute("SELECT filled_size FROM live_order_intents").fetchone()[0] == "4"

    client.trades = [exchange_trade(size="2")]
    lifecycle.reconcile()
    with executor._db() as db:
        assert db.execute("SELECT filled_size FROM live_order_intents").fetchone()[0] == "4"
        assert db.execute("SELECT COUNT(*) FROM live_fills").fetchone()[0] == 1
