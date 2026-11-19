"""Fail-closed lifecycle manager for the dormant Polymarket live adapter.

This module is intentionally not imported by the strategy loop. It supports
read/reconcile/cancel/exit workflows for isolated tests and future review.
No network write is automatically retried after an ambiguous outcome.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Callable

from src.execution.live import (
    DuplicateLiveIntent,
    LiveExecutionDisabled,
    LiveExecutionError,
    LiveSubmissionUnknown,
    PER_ORDER_APPROVAL,
    USDC_BASE_UNITS,
    _decimal,
    _field,
)

logger = logging.getLogger("astf.execution.lifecycle")
TERMINAL = {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}
ACTIVE = {"SUBMITTING", "UNKNOWN", "ACCEPTED", "PARTIALLY_FILLED", "CANCEL_REQUESTED", "CANCEL_UNKNOWN"}


def _iso(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value) if value else None


def _retry_after(exc) -> float | None:
    headers = getattr(getattr(exc, "response", None), "headers", {}) or {}
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, min(30.0, float(raw)))
    except (TypeError, ValueError):
        try:
            dt = parsedate_to_datetime(str(raw))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, min(30.0, (dt - datetime.now(timezone.utc)).total_seconds()))
        except Exception:
            return None


class LiveLifecycle:
    """Reconciles durable intents against authenticated CLOB and portfolio data."""

    def __init__(self, executor, *, read_attempts=3, backoff_seconds=0.25,
                 sleep: Callable[[float], None] = time.sleep):
        self.executor = executor
        self.client = executor.client
        self.config = executor.config
        self.read_attempts = max(1, min(5, int(read_attempts)))
        self.backoff_seconds = max(0.0, float(backoff_seconds))
        self.sleep = sleep
        self.positions: dict[str, dict] = {}
        self.open_orders: dict[str, object] = {}
        self.last_reconcile_at: datetime | None = None
        self.last_reconcile_ok = False
        self._initialize()

    def _initialize(self):
        with self.executor._db() as db:
            cols = {r[1] for r in db.execute("PRAGMA table_info(live_order_intents)")}
            additions = {
                "expires_at": "TEXT",
                "filled_size": "TEXT NOT NULL DEFAULT '0'",
                "last_reconciled_at": "TEXT",
                "terminal_reason": "TEXT",
            }
            for name, typ in additions.items():
                if name not in cols:
                    db.execute(f"ALTER TABLE live_order_intents ADD COLUMN {name} {typ}")
            db.execute("""CREATE TABLE IF NOT EXISTS live_fills (
                trade_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, asset_id TEXT NOT NULL,
                side TEXT NOT NULL, price TEXT NOT NULL, size TEXT NOT NULL,
                fee_rate_bps TEXT, matched_at TEXT, raw_json TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS live_position_snapshots (
                asset_id TEXT PRIMARY KEY, condition_id TEXT, shares TEXT NOT NULL,
                avg_price TEXT NOT NULL, cost_usdc TEXT NOT NULL, current_value TEXT NOT NULL,
                unrealized_pnl TEXT NOT NULL, realized_pnl TEXT NOT NULL,
                status TEXT NOT NULL, observed_at TEXT NOT NULL)""")

    def _read(self, fn, *args, **kwargs):
        """Bounded retries for reads only; never use for order submit/cancel."""
        for attempt in range(self.read_attempts):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                name = type(exc).__name__.lower()
                retryable = any(x in name for x in (
                    "ratelimit", "rate_limit", "transport", "timeout",
                    "connection", "temporarilyunavailable",
                ))
                if not retryable or attempt + 1 >= self.read_attempts:
                    raise
                delay = _retry_after(exc)
                if delay is None:
                    delay = min(4.0, self.backoff_seconds * (2 ** attempt))
                logger.warning("Transient read failure (%s); retry %d/%d in %.2fs",
                               type(exc).__name__, attempt + 1, self.read_attempts - 1, delay)
                self.sleep(delay)

    @staticmethod
    def _items(paginator, max_items=10000):
        if hasattr(paginator, "iter_items"):
            iterator = paginator.iter_items()
        elif hasattr(paginator, "first_page"):
            page = paginator.first_page()
            iterator = iter(_field(page, "items", ()) or ())
        else:
            raise LiveExecutionError("SDK paginator has no supported iteration method")
        out = []
        for item in iterator:
            out.append(item)
            if len(out) > max_items:
                raise LiveExecutionError("Exchange pagination exceeded safety cap; reconciliation incomplete")
        return out

    def _read_items(self, factory, *args, **kwargs):
        """Retry a complete paginated GET from page one after transient failures."""
        for attempt in range(self.read_attempts):
            try:
                paginator = self._read(factory, *args, **kwargs)
                return self._items(paginator)
            except Exception as exc:
                name = type(exc).__name__.lower()
                retryable = any(x in name for x in (
                    "ratelimit", "rate_limit", "transport", "timeout",
                    "connection", "temporarilyunavailable",
                ))
                if not retryable or attempt + 1 >= self.read_attempts:
                    raise
                delay = _retry_after(exc)
                if delay is None:
                    delay = min(4.0, self.backoff_seconds * (2 ** attempt))
                logger.warning("Paginated read failed (%s); restarting from first page in %.2fs",
                               type(exc).__name__, delay)
                self.sleep(delay)

    def _intent_rows(self):
        with self.executor._db() as db:
            db.row_factory = __import__("sqlite3").Row
            return [dict(r) for r in db.execute(
                "SELECT * FROM live_order_intents WHERE status NOT IN ('REJECTED','CANCELED','EXPIRED')"
            ).fetchall()]

    def _record_trade(self, trade, intents_by_id, unmatched_intents=()):
        trade_id = str(_field(trade, "id", "") or "")
        if not trade_id:
            raise LiveExecutionError("Exchange trade missing immutable trade ID")
        asset_id = str(_field(trade, "asset_id", _field(trade, "token_id", "")) or "")
        order_id = str(_field(trade, "taker_order_id", "") or "")
        side = str(_field(trade, "side", "") or "").upper()
        price = _decimal(_field(trade, "price"), "trade price")
        size = _decimal(_field(trade, "size"), "trade size")
        fee_bps = _field(trade, "fee_rate_bps")
        matched_at = _iso(_field(trade, "matched_at"))
        maker_orders = _field(trade, "maker_orders", ()) or ()
        matched_order = intents_by_id.get(order_id)
        if matched_order is None and order_id:
            # Recover a fully filled order whose submit acknowledgement and
            # open-order visibility were both lost. Require a unique exact
            # asset/side/price fingerprint and a plausible partial quantity.
            candidates = [
                r for r in unmatched_intents
                if not r.get("order_id")
                and str(r.get("asset_id")) == asset_id
                and str(r.get("side", "")).upper() == side
                and _decimal(r.get("price"), "intent price") == price
                and size <= _decimal(r.get("size"), "intent size")
            ]
            if len(candidates) == 1:
                matched_order = candidates[0]
                matched_order["order_id"] = order_id
                with self.executor._db() as db:
                    db.execute("UPDATE live_order_intents SET order_id=?,status='UNKNOWN',terminal_reason=? WHERE intent_key=?",
                               (order_id, "Order ID recovered from unique matching account fill", matched_order["intent_key"]))
                intents_by_id[order_id] = matched_order
        if matched_order is None:
            for maker in maker_orders:
                candidate = str(_field(maker, "order_id", "") or "")
                if candidate in intents_by_id:
                    matched_order = intents_by_id[candidate]
                    order_id = candidate
                    side = str(_field(maker, "side", "") or "").upper()
                    price = _decimal(_field(maker, "price"), "maker fill price")
                    size = _decimal(_field(maker, "matched_amount"), "maker matched amount")
                    break
        if matched_order is None:
            return False
        if side not in {"BUY", "SELL"} or size <= 0 or not (0 < price < 1):
            raise LiveExecutionError("Invalid exchange fill fields; reconciliation halted")
        raw = json.dumps({
            "id": trade_id, "order_id": order_id, "asset_id": asset_id,
            "side": side, "price": str(price), "size": str(size),
            "fee_rate_bps": str(fee_bps) if fee_bps is not None else None,
            "matched_at": matched_at,
        }, sort_keys=True)
        with self.executor._db() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO live_fills(trade_id,order_id,asset_id,side,price,size,fee_rate_bps,matched_at,raw_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (trade_id, order_id, asset_id, side, str(price), str(size),
                 str(fee_bps) if fee_bps is not None else None, matched_at, raw),
            )
            if cur.rowcount == 0:
                return False
            # Fill rows are an immutable audit trail. Fees are retained as
            # exchange-reported bps; do not invent a fee formula/net P&L here.
            # Filled quantity is derived from the exchange's current open-order
            # matched size, or from deduplicated trade rows once the order is
            # terminal. Never add trade totals to a matched-size snapshot.
            db.execute("UPDATE live_order_intents SET last_reconciled_at=? WHERE intent_key=?",
                       (datetime.now(timezone.utc).isoformat(), matched_order["intent_key"]))
        return True

    def reconcile(self) -> dict:
        """Refresh all persisted active intents, account fills and wallet positions."""
        self.last_reconcile_ok = False
        rows = self._intent_rows()
        asset_ids = sorted({str(r["asset_id"]) for r in rows if r.get("asset_id")})
        open_items = self._read_items(self.client.list_open_orders)
        self.open_orders = {str(_field(o, "id")): o for o in open_items}

        # Recover a lost submit acknowledgement only when exactly one exchange
        # order matches the persisted immutable intent attributes.
        with self.executor._db() as db:
            for row in rows:
                if row.get("order_id") or row.get("status") not in {"SUBMITTING", "UNKNOWN"}:
                    continue
                matches = [
                    o for o in open_items
                    if str(_field(o, "asset_id", _field(o, "token_id", ""))) == str(row["asset_id"])
                    and str(_field(o, "side", "")).upper() == str(row["side"]).upper()
                    and _decimal(_field(o, "price"), "recovery order price") == _decimal(row["price"], "intent price")
                    and _decimal(_field(o, "original_size"), "recovery order size") == _decimal(row["size"], "intent size")
                ]
                if len(matches) == 1:
                    recovered_id = str(_field(matches[0], "id"))
                    db.execute("UPDATE live_order_intents SET order_id=?,status='ACCEPTED',terminal_reason=? WHERE intent_key=?",
                               (recovered_id, "Recovered by exact exchange order fingerprint after lost acknowledgement", row["intent_key"]))
                    row["order_id"] = recovered_id
                    row["status"] = "ACCEPTED"
                elif len(matches) > 1:
                    logger.error("Ambiguous recovery for intent %s; manual reconciliation required", row["intent_key"])
                    with self.executor._db() as db:
                        db.execute("UPDATE live_order_intents SET status='NOT_OPEN_RECONCILE_REQUIRED',terminal_reason=? WHERE intent_key=?",
                                   ("Multiple exchange orders match intent fingerprint; manual reconciliation required", row["intent_key"]))
                    row["status"] = "NOT_OPEN_RECONCILE_REQUIRED"

        intents_by_id = {str(r.get("order_id")): r for r in rows if r.get("order_id")}
        new_fills = 0
        for asset_id in asset_ids:
            trades = self._read_items(self.client.list_account_trades, asset_id=asset_id)
            for trade in trades:
                unmatched = [r for r in rows if not r.get("order_id")]
                if self._record_trade(trade, intents_by_id, unmatched):
                    new_fills += 1

        # Wallet positions are authoritative for current holdings. Persist the
        # exact SDK snapshot; don't infer balances from order acknowledgements.
        position_items = self._read_items(self.client.list_positions, status="OPEN", full_history=True)
        now = datetime.now(timezone.utc).isoformat()
        positions = {}
        with self.executor._db() as db:
            for p in position_items:
                asset_id = str(_field(p, "asset_id", _field(p, "token_id", "")) or "")
                shares = _decimal(_field(p, "current_size"), "position shares")
                if not asset_id or shares <= 0:
                    continue
                row = {
                    "asset_id": asset_id,
                    "condition_id": str(_field(p, "condition_id", "") or ""),
                    "shares": shares,
                    "avg_price": _decimal(_field(p, "avg_price"), "position average price"),
                    "cost_usdc": _decimal(_field(p, "total_cost_usdc"), "position cost"),
                    "current_value": _decimal(_field(p, "current_value"), "position value"),
                    "unrealized_pnl": _decimal(_field(p, "unrealized_pnl"), "unrealized pnl"),
                    "realized_pnl": _decimal(_field(p, "realized_pnl"), "realized pnl"),
                    "status": str(_field(p, "status", "OPEN")),
                }
                positions[asset_id] = row
                db.execute("""INSERT INTO live_position_snapshots
                    (asset_id,condition_id,shares,avg_price,cost_usdc,current_value,unrealized_pnl,realized_pnl,status,observed_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(asset_id) DO UPDATE SET condition_id=excluded.condition_id,
                    shares=excluded.shares,avg_price=excluded.avg_price,cost_usdc=excluded.cost_usdc,
                    current_value=excluded.current_value,unrealized_pnl=excluded.unrealized_pnl,
                    realized_pnl=excluded.realized_pnl,status=excluded.status,observed_at=excluded.observed_at""",
                    (asset_id, row["condition_id"], str(row["shares"]), str(row["avg_price"]),
                     str(row["cost_usdc"]), str(row["current_value"]), str(row["unrealized_pnl"]),
                     str(row["realized_pnl"]), row["status"], now))
            # Remove stale local snapshots only after a complete successful
            # authoritative page traversal.
            if positions:
                placeholders = ",".join("?" for _ in positions)
                db.execute(f"DELETE FROM live_position_snapshots WHERE asset_id NOT IN ({placeholders})",
                           tuple(positions))
            else:
                db.execute("DELETE FROM live_position_snapshots")

            for row in rows:
                oid = str(row.get("order_id") or "")
                if not oid:
                    continue
                open_order = self.open_orders.get(oid)
                if open_order is None:
                    # A complete account-trade history covering the order's
                    # full original quantity proves it is filled. Otherwise
                    # absence alone does not prove cancellation or expiry.
                    fill_sum = db.execute("SELECT COALESCE(SUM(CAST(size AS REAL)),0) FROM live_fills WHERE order_id=?",
                                          (oid,)).fetchone()[0]
                    filled = _decimal(fill_sum, "deduplicated fill quantity")
                    original = _decimal(row.get("size", "0"), "original order quantity")
                    if original > 0 and filled >= original:
                        status, reason = "FILLED", "Account trade fills cover the original order quantity"
                    elif row.get("status") == "CANCEL_REQUESTED":
                        status, reason = "CANCELED", "Cancel was acknowledged and order is absent from complete open-order listing"
                    elif row.get("expires_at"):
                        try:
                            expiry = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
                            if expiry.tzinfo is not None and expiry <= datetime.now(timezone.utc):
                                status, reason = "EXPIRED", "GTD expiry passed and order is absent from complete open-order listing"
                            else:
                                status, reason = "NOT_OPEN_RECONCILE_REQUIRED", "Absent from open-order listing; terminal reason not yet proven"
                        except (TypeError, ValueError):
                            status, reason = "NOT_OPEN_RECONCILE_REQUIRED", "Unparseable expiry; manual reconciliation required"
                    else:
                        status = "NOT_OPEN_RECONCILE_REQUIRED"
                        reason = "Absent from open-order listing; terminal reason not yet proven"
                else:
                    original = _decimal(_field(open_order, "original_size"), "original order size")
                    matched = _decimal(_field(open_order, "size_matched"), "matched order size")
                    if matched >= original:
                        status, reason = "FILLED", "Exchange open-order record reports fully matched"
                    elif matched > 0:
                        status, reason = "PARTIALLY_FILLED", None
                    else:
                        status, reason = "ACCEPTED", None
                db.execute("""UPDATE live_order_intents SET status=?,filled_size=?,expires_at=?,
                    last_reconciled_at=?,terminal_reason=? WHERE intent_key=?""",
                    (status, str(_field(open_order, "size_matched", row.get("filled_size", "0"))) if open_order else row.get("filled_size", "0"),
                     _iso(_field(open_order, "expires_at")) if open_order else row.get("expires_at"),
                     now, reason, row["intent_key"]))
        self.positions = positions
        self.last_reconcile_at = datetime.now(timezone.utc)
        self.last_reconcile_ok = True
        return {"open_orders": len(self.open_orders), "positions": len(self.positions),
                "new_fills": new_fills, "reconciled_at": self.last_reconcile_at.isoformat()}

    def run_maintenance(self) -> dict:
        """Restart-safe read reconciliation followed by expiry cancellation."""
        snapshot = self.reconcile()
        snapshot["cancel_requested"] = self.cancel_expired_orders()
        return snapshot

    def cancel_expired_orders(self, *, safety_seconds=5) -> list[str]:
        """Cancel orders nearing/at GTD expiry; verify absence on next reconcile."""
        if not self.last_reconcile_ok or self.last_reconcile_at is None:
            raise LiveExecutionError("Successful reconciliation required before cancellation")
        now = datetime.now(timezone.utc)
        canceled = []
        for order_id, order in list(self.open_orders.items()):
            expiry = _field(order, "expires_at")
            if not isinstance(expiry, datetime):
                continue
            if expiry.tzinfo is None:
                raise LiveExecutionError("Naive order expiry timestamp; refusing cancellation decision")
            if expiry > now + timedelta(seconds=max(0, safety_seconds)):
                continue
            try:
                response = self.client.cancel_order(order_id=order_id)
            except Exception:
                self._set_order_status(order_id, "CANCEL_UNKNOWN", "Cancel response ambiguous; reconcile before any retry")
                continue
            canceled_ids = set(str(x) for x in (_field(response, "canceled", ()) or ()))
            not_canceled = _field(response, "not_canceled", {}) or {}
            if order_id in canceled_ids:
                self._set_order_status(order_id, "CANCEL_REQUESTED", "Exchange acknowledged cancel; verify next cycle")
                canceled.append(order_id)
            else:
                reason = not_canceled.get(order_id, "Exchange did not confirm cancellation")
                self._set_order_status(order_id, "CANCEL_UNKNOWN", str(reason))
        return canceled

    def _set_order_status(self, order_id, status, reason):
        with self.executor._db() as db:
            db.execute("UPDATE live_order_intents SET status=?,terminal_reason=?,last_reconciled_at=? WHERE order_id=?",
                       (status, reason, datetime.now(timezone.utc).isoformat(), order_id))

    def submit_exit_limit(self, *, intent_key: str, asset_id: str, price, shares=None,
                          approval: str) -> dict:
        """Submit a passive SELL against a reconciled wallet position.

        This is not a guaranteed emergency exit: it will not cross the spread.
        """
        if approval != PER_ORDER_APPROVAL:
            raise LiveExecutionDisabled("Per-order approval token is required for exit")
        if not self.last_reconcile_ok:
            raise LiveExecutionError("Reconcile wallet positions immediately before exit")
        position = self.positions.get(str(asset_id))
        if not position:
            raise LiveExecutionError("No reconciled open position for this asset")
        px = _decimal(price, "exit price")
        available_shares = position["shares"]
        qty = available_shares if shares is None else _decimal(shares, "exit shares")
        if not (0 < px < 1) or qty <= 0 or qty > available_shares:
            raise LiveExecutionError("Exit price/quantity invalid or exceeds held shares")
        book = self._read(self.client.get_order_book, asset_id=str(asset_id))
        stamp = _field(book, "timestamp")
        if not isinstance(stamp, datetime) or stamp.tzinfo is None:
            raise LiveExecutionError("Fresh timezone-aware order book required")
        age = (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()
        if age < -1 or age > float(self.config.get("max_book_age_seconds", 3)):
            raise LiveExecutionError("Stale/clock-skewed exit book")
        bids, asks = list(_field(book, "bids", ()) or ()), list(_field(book, "asks", ()) or ())
        if not bids or not asks:
            raise LiveExecutionError("Both sides of book required for exit")
        best_bid = max(_decimal(_field(x, "price"), "bid") for x in bids)
        best_ask = min(_decimal(_field(x, "price"), "ask") for x in asks)
        if best_ask <= best_bid or best_ask - best_bid > _decimal(self.config.get("max_spread", "0.05"), "max spread"):
            raise LiveExecutionError("Crossed book or exit spread cap exceeded")
        if px <= best_bid:
            raise LiveExecutionError("SELL would cross/lock bid; passive-only exit refused")
        if px * qty > _decimal(self.config.get("max_order_notional_usd", 10), "max order notional"):
            raise LiveExecutionError("Exit order notional cap exceeded")
        if len(self.open_orders) >= int(self.config.get("max_open_orders", 1)):
            raise LiveExecutionError("Open-order cap reached before exit")
        if qty < _decimal(_field(book, "min_order_size", 0), "minimum order size"):
            raise LiveExecutionError("Exit is below market minimum size")
        balance = self._read(self.client.get_balance_allowance, asset_type="CONDITIONAL", asset_id=str(asset_id))
        token_balance = _decimal(_field(balance, "balance", 0), "conditional token balance") / USDC_BASE_UNITS
        allowances = _field(balance, "allowances", {}) or {}
        allowance = max((_decimal(v, "conditional allowance") for v in allowances.values()), default=Decimal("0")) / USDC_BASE_UNITS
        if token_balance < qty or allowance < qty:
            raise LiveExecutionError("Insufficient token balance/allowance; no automatic approval")
        if any(str(_field(o, "asset_id", "")) == str(asset_id) and str(_field(o, "side", "")).upper() == "SELL"
               for o in self.open_orders.values()):
            raise LiveExecutionError("Existing SELL order already reserves this position")
        if not intent_key or len(intent_key) < 8:
            raise LiveExecutionError("Unique durable exit intent key required")
        self.executor._reserve(str(intent_key), str(asset_id), px, qty)
        with self.executor._db() as db:
            db.execute("UPDATE live_order_intents SET side='SELL' WHERE intent_key=?", (str(intent_key),))
        expiry = int(time.time()) + max(600, int(self.config.get("order_ttl_seconds", 600)))
        try:
            signed = self.client.create_limit_order(asset_id=str(asset_id), price=px, size=qty,
                                                    side="SELL", post_only=True, expiration=expiry)
            response = self.client.post_order(signed)
        except Exception as exc:
            self.executor._update(str(intent_key), "UNKNOWN")
            raise LiveSubmissionUnknown("Exit submission outcome unknown; reconcile before retry") from exc
        if bool(_field(response, "ok", False)) and _field(response, "order_id"):
            oid = str(_field(response, "order_id"))
            result = {"ok": True, "order_id": oid, "status": str(_field(response, "status", "accepted")),
                      "intent_key": str(intent_key), "side": "SELL", "post_only": True}
            self.executor._update(str(intent_key), "ACCEPTED", oid, result)
            with self.executor._db() as db:
                db.execute("UPDATE live_order_intents SET expires_at=? WHERE intent_key=?",
                           (datetime.fromtimestamp(expiry, timezone.utc).isoformat(), str(intent_key)))
            return result
        self.executor._update(str(intent_key), "REJECTED", response={"message": str(_field(response, "message", "rejected"))})
        return {"ok": False, "intent_key": str(intent_key), "message": str(_field(response, "message", "rejected"))}
