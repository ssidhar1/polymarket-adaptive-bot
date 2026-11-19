"""Dormant, fail-closed Polymarket live order adapter.

Not connected to ASTF's signal loop. Paper remains the only supported runtime
until partial-fill reconciliation, exits, cancellation, and restart recovery
are implemented and separately reviewed.
"""
from __future__ import annotations
import logging, os, re, sqlite3, time
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping

logger = logging.getLogger("astf.execution.live")
LIVE_ENABLE_VALUE = "YES"
LIVE_CONFIRM_VALUE = "I_ACCEPT_REAL_MONEY_RISK"
PER_ORDER_APPROVAL = "SUBMIT_ONE_LIVE_ORDER"
USDC_BASE_UNITS = Decimal("1000000")


class LiveExecutionError(RuntimeError): pass
class LiveExecutionDisabled(LiveExecutionError): pass
class DuplicateLiveIntent(LiveExecutionError): pass
class LiveSubmissionUnknown(LiveExecutionError): pass


def _decimal(value, name):
    try: result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError): raise LiveExecutionError(f"Invalid {name}") from None
    if not result.is_finite(): raise LiveExecutionError(f"Invalid {name}")
    return result


def _field(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


class LiveExecutor:
    """Single-order adapter: passive BUY limits only; never auto-approves."""

    def __init__(self, client, config: dict, journal_path: str | Path, environ: Mapping[str, str] | None = None):
        self.env = os.environ if environ is None else environ
        self.config = (config or {}).get("live_execution", {}) or {}
        self._assert_armed(self.config, self.env)
        if client is None: raise LiveExecutionDisabled("Authenticated official SDK client is required")
        self.client = client
        self.journal_path = Path(journal_path)
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_journal()

    @staticmethod
    def _assert_armed(config, env):
        if config.get("enabled") is not True: raise LiveExecutionDisabled("live_execution.enabled must be true")
        if env.get("ASTF_ENABLE_LIVE_TRADING") != LIVE_ENABLE_VALUE: raise LiveExecutionDisabled("ASTF_ENABLE_LIVE_TRADING=YES is required")
        if env.get("ASTF_LIVE_CONFIRMATION") != LIVE_CONFIRM_VALUE: raise LiveExecutionDisabled("Explicit live confirmation is missing")
        if env.get("DEFAULT_MODE", "paper").lower() != "live": raise LiveExecutionDisabled("DEFAULT_MODE must explicitly be live")

    @classmethod
    def from_env(cls, config, journal_path, environ=None):
        """Create SDK client only after gates pass; EOA only, no wallet deployment."""
        env = os.environ if environ is None else environ
        cls._assert_armed((config or {}).get("live_execution", {}) or {}, env)
        key, wallet = env.get("POLYMARKET_PRIVATE_KEY", ""), env.get("POLYMARKET_WALLET", "")
        if not key or not wallet: raise LiveExecutionDisabled("POLYMARKET_PRIVATE_KEY and POLYMARKET_WALLET are required")
        try:
            from eth_account import Account
            from polymarket import SecureClient
            signer = Account.from_key(key).address
        except Exception as exc:
            raise LiveExecutionError(f"Official Polymarket SDK initialization failed: {exc}") from exc
        if signer.lower() != wallet.lower():
            raise LiveExecutionDisabled("Only an EOA wallet matching the signing key is supported; smart/deposit wallets need separate review")
        client = SecureClient.create(private_key=key, wallet=wallet)
        return cls(client, config, journal_path, environ=env)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(str(self.journal_path), timeout=10)
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _initialize_journal(self):
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS live_order_intents (
                intent_key TEXT PRIMARY KEY, status TEXT NOT NULL, asset_id TEXT NOT NULL,
                side TEXT NOT NULL, price TEXT NOT NULL, size TEXT NOT NULL, order_id TEXT,
                response_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(live_order_intents)")}
            if "expires_at" not in columns:
                db.execute("ALTER TABLE live_order_intents ADD COLUMN expires_at TEXT")

    def _reserve(self, key, asset, price, size):
        now = datetime.now(timezone.utc).isoformat()
        with self._db() as db:
            row = db.execute("SELECT status FROM live_order_intents WHERE intent_key=?", (key,)).fetchone()
            if row: raise DuplicateLiveIntent(f"Intent already exists ({row[0]}); reconcile, never resubmit")
            db.execute("INSERT INTO live_order_intents(intent_key,status,asset_id,side,price,size,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                       (key, "SUBMITTING", asset, "BUY", str(price), str(size), now, now))

    def _update(self, key, status, order_id=None, response=None):
        import json
        with self._db() as db:
            db.execute("UPDATE live_order_intents SET status=?,order_id=?,response_json=?,updated_at=? WHERE intent_key=?",
                       (status, order_id, json.dumps(response, default=str) if response is not None else None,
                        datetime.now(timezone.utc).isoformat(), key))

    def submit_buy_limit(self, *, intent_key, asset_id, price, size, bankroll_usd,
                         risk_paused: bool, approval: str) -> dict:
        if approval != PER_ORDER_APPROVAL: raise LiveExecutionDisabled("Per-order approval token is required")
        if risk_paused: raise LiveExecutionError("Risk manager is paused")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", str(intent_key or "")): raise LiveExecutionError("Invalid intent_key")
        if not asset_id or len(str(asset_id)) < 10: raise LiveExecutionError("Valid CLOB asset_id required")

        px, shares, bankroll = _decimal(price, "price"), _decimal(size, "size"), _decimal(bankroll_usd, "bankroll")
        notional = px * shares
        max_order = _decimal(self.config.get("max_order_notional_usd", 10), "max_order_notional_usd")
        max_position_pct = _decimal(self.config.get("max_position_pct", "0.01"), "max_position_pct")
        max_total_pct = _decimal(self.config.get("max_total_exposure_pct", "0.05"), "max_total_exposure_pct")
        max_open = int(self.config.get("max_open_orders", 1))
        max_age = float(self.config.get("max_book_age_seconds", 3))
        max_spread = _decimal(self.config.get("max_spread", "0.05"), "max_spread")
        if not (0 < px < 1) or shares <= 0 or bankroll <= 0: raise LiveExecutionError("Price, size, or bankroll invalid")
        if min(max_order, max_position_pct, max_total_pct) <= 0: raise LiveExecutionError("Live limits must be positive")
        if notional > min(max_order, bankroll * max_position_pct): raise LiveExecutionError("Per-order notional cap exceeded")

        book = self.client.get_order_book(asset_id=str(asset_id))
        stamp = _field(book, "timestamp")
        if not isinstance(stamp, datetime) or stamp.tzinfo is None: raise LiveExecutionError("Book timestamp missing or timezone-naive")
        age = (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()
        if age < -1 or age > max_age: raise LiveExecutionError(f"Stale/clock-skewed book ({age:.2f}s)")
        bids, asks = list(_field(book, "bids", ()) or ()), list(_field(book, "asks", ()) or ())
        if not bids or not asks: raise LiveExecutionError("Both sides of book required")
        best_bid = max(_decimal(_field(x, "price"), "bid") for x in bids)
        best_ask = min(_decimal(_field(x, "price"), "ask") for x in asks)
        if best_ask <= best_bid: raise LiveExecutionError("Crossed/locked book")
        if best_ask - best_bid > max_spread: raise LiveExecutionError("Spread cap exceeded")
        if px >= best_ask: raise LiveExecutionError("BUY would cross ask; passive-only order refused")
        if shares < _decimal(_field(book, "min_order_size", 0), "minimum size"): raise LiveExecutionError("Below market minimum size")

        page = self.client.list_open_orders().first_page()
        orders = list(_field(page, "items", ()) or ())
        if len(orders) >= max_open: raise LiveExecutionError("Open-order cap reached")
        reserved = Decimal("0")
        for order in orders:
            if str(_field(order, "side", "")).upper() == "BUY":
                original = _decimal(_field(order, "original_size", 0), "open size")
                matched = _decimal(_field(order, "size_matched", 0), "matched size")
                opx = _decimal(_field(order, "price", 0), "open price")
                reserved += max(Decimal("0"), original - matched) * opx
        # Include already-held positions, not just resting orders. A full first
        # page is treated as incomplete data and therefore blocks new exposure.
        position_page = self.client.list_positions(status="OPEN").first_page()
        positions = list(_field(position_page, "items", ()) or ())
        if len(positions) >= 100:
            raise LiveExecutionError("Position page may be truncated; refusing new exposure")
        held = Decimal("0")
        for position in positions:
            current_value = _decimal(_field(position, "current_value", 0), "position value")
            cost_basis = _decimal(_field(position, "total_cost_usdc", 0), "position cost")
            held += max(current_value, cost_basis)
        if reserved + held + notional > bankroll * max_total_pct:
            raise LiveExecutionError("Portfolio exposure cap exceeded")

        bal = self.client.get_balance_allowance(asset_type="COLLATERAL")
        available = _decimal(_field(bal, "balance", 0), "collateral balance") / USDC_BASE_UNITS
        allowances = _field(bal, "allowances", {}) or {}
        allowance = max((_decimal(v, "allowance") for v in allowances.values()), default=Decimal("0")) / USDC_BASE_UNITS
        if available < notional * Decimal("1.02"): raise LiveExecutionError("Insufficient collateral including fee buffer")
        if allowance < notional: raise LiveExecutionError("Existing allowance insufficient; auto-approval disabled")

        # Reserve durable idempotency key before network submission.
        self._reserve(str(intent_key), str(asset_id), px, shares)
        expiry = int(time.time()) + max(600, int(self.config.get("order_ttl_seconds", 600)))
        try:
            signed = self.client.create_limit_order(asset_id=str(asset_id), price=px, size=shares,
                                                    side="BUY", post_only=True, expiration=expiry)
        except Exception as exc:
            self._update(str(intent_key), "REJECTED", response={"stage": "sign", "error": type(exc).__name__})
            raise LiveExecutionError("Order signing failed before network submission") from exc
        try:
            # post_order avoids SDK automatic allowance-recovery side effects.
            response = self.client.post_order(signed)
        except Exception as exc:
            self._update(str(intent_key), "UNKNOWN")
            raise LiveSubmissionUnknown("Submit outcome unknown; reconcile orders/trades, never retry this key") from exc

        ok, order_id = bool(_field(response, "ok", False)), _field(response, "order_id")
        if ok and order_id:
            result = {"ok": True, "status": str(_field(response, "status", "accepted")),
                      "order_id": str(order_id), "trade_ids": list(_field(response, "trade_ids", ()) or ()),
                      "intent_key": str(intent_key), "post_only": True}
            self._update(str(intent_key), "ACCEPTED", str(order_id), result)
            with self._db() as db:
                db.execute("UPDATE live_order_intents SET expires_at=? WHERE intent_key=?",
                           (datetime.fromtimestamp(expiry, timezone.utc).isoformat(), str(intent_key)))
            logger.warning("LIVE ORDER ACCEPTED | order_id=%s | intent=%s", order_id, intent_key)
            return result
        result = {"ok": False, "message": str(_field(response, "message", "CLOB rejected order")),
                  "intent_key": str(intent_key)}
        self._update(str(intent_key), "REJECTED", response=result)
        return result
