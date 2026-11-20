"""Append-only, timestamp-aware journal for public Polymarket CLOB market events.

The journal stores the original payload and both exchange and local receive time.
It deliberately does not synthesize missing deltas or claim a book is complete
without a prior snapshot. It is a research/paper-data component only.
"""
import hashlib
import json
import sqlite3
import time
from pathlib import Path


class MarketEventError(ValueError):
    pass


def _timestamp_ms(value):
    try:
        stamp = int(float(value))
    except (TypeError, ValueError, OverflowError):
        raise MarketEventError("event is missing a valid exchange timestamp")
    if stamp <= 0:
        raise MarketEventError("exchange timestamp must be positive")
    return stamp


def _levels(rows):
    out = {}
    for row in rows or []:
        try:
            price = float(row.get("price"))
            size = float(row.get("size"))
        except (AttributeError, TypeError, ValueError):
            continue
        if 0 < price < 1 and size >= 0:
            out[price] = size
    return out


class MarketEventJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self):
        db = sqlite3.connect(str(self.path), timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def _init_db(self):
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("""CREATE TABLE IF NOT EXISTS market_events (
                ingest_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                market_id TEXT NOT NULL DEFAULT '',
                asset_id TEXT NOT NULL DEFAULT '',
                exchange_ts_ms INTEGER NOT NULL,
                received_ts_ns INTEGER NOT NULL,
                payload_sha256 TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_market_events_asset_time ON market_events(asset_id, exchange_ts_ms, ingest_seq)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_market_events_market_time ON market_events(market_id, exchange_ts_ms, ingest_seq)")

    def append(self, payload, received_ts_ns=None):
        if not isinstance(payload, dict):
            raise MarketEventError("market event must be a JSON object")
        event_type = str(payload.get("event_type") or "").strip()
        if not event_type:
            raise MarketEventError("event_type is required")
        exchange_ts_ms = _timestamp_ms(payload.get("timestamp"))
        changes = payload.get("price_changes") or payload.get("changes") or []
        asset_id = str(payload.get("asset_id") or (changes[0].get("asset_id") if changes and isinstance(changes[0], dict) else "") or "")
        market_id = str(payload.get("market") or payload.get("condition_id") or "")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        received = int(received_ts_ns if received_ts_ns is not None else time.time_ns())
        with self._connect() as db:
            cur = db.execute("""INSERT INTO market_events
                (event_type,market_id,asset_id,exchange_ts_ms,received_ts_ns,payload_sha256,payload_json)
                VALUES(?,?,?,?,?,?,?)""",
                (event_type, market_id, asset_id, exchange_ts_ms, received, digest, canonical))
            return {"ingest_seq": cur.lastrowid, "event_type": event_type,
                    "market_id": market_id, "asset_id": asset_id,
                    "exchange_ts_ms": exchange_ts_ms, "received_ts_ns": received,
                    "payload_sha256": digest}

    def replay_book(self, asset_id, *, as_of_ms=None, received_before_ns=None):
        """Rebuild a book using only events received by the requested decision time."""
        clauses = ["asset_id=?"]
        args = [str(asset_id)]
        if as_of_ms is not None:
            clauses.append("exchange_ts_ms<=?")
            args.append(int(as_of_ms))
        if received_before_ns is not None:
            clauses.append("received_ts_ns<=?")
            args.append(int(received_before_ns))
        query = ("SELECT * FROM market_events WHERE " + " AND ".join(clauses)
                 + " ORDER BY exchange_ts_ms ASC, ingest_seq ASC")
        bids, asks = {}, {}
        has_snapshot = False
        last_exchange_ts = None
        out_of_order = False
        applied = 0
        with self._connect() as db:
            rows = db.execute(query, args).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            event_type = row["event_type"]
            stamp = int(row["exchange_ts_ms"])
            if last_exchange_ts is not None and stamp < last_exchange_ts:
                out_of_order = True
                continue
            if event_type == "book":
                bids = _levels(payload.get("bids") or payload.get("buys") or [])
                asks = _levels(payload.get("asks") or payload.get("sells") or [])
                has_snapshot = True
                applied += 1
            elif event_type == "price_change" and has_snapshot:
                changes = payload.get("price_changes") or payload.get("changes") or []
                for change in changes:
                    if str(change.get("asset_id") or row["asset_id"]) != str(asset_id):
                        continue
                    side = str(change.get("side") or "").upper()
                    book_side = bids if side in ("BUY", "BID") else asks if side in ("SELL", "ASK") else None
                    if book_side is None:
                        continue
                    try:
                        price, size = float(change["price"]), float(change["size"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if not 0 < price < 1 or size < 0:
                        continue
                    if size == 0:
                        book_side.pop(price, None)
                    else:
                        book_side[price] = size
                    applied += 1
            last_exchange_ts = stamp if last_exchange_ts is None else max(last_exchange_ts, stamp)
        return {
            "asset_id": str(asset_id),
            "bids": [{"price": p, "size": bids[p]} for p in sorted(bids, reverse=True)],
            "asks": [{"price": p, "size": asks[p]} for p in sorted(asks)],
            "has_snapshot": has_snapshot,
            "complete": has_snapshot and not out_of_order,
            "out_of_order": out_of_order,
            "applied_events": applied,
            "last_exchange_ts_ms": last_exchange_ts,
        }

    def count(self):
        with self._connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM market_events").fetchone()[0])
