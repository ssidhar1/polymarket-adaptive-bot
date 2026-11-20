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
            db.execute("""CREATE TABLE IF NOT EXISTS market_connection_gaps (
                gap_id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_id TEXT NOT NULL,
                received_ts_ns INTEGER NOT NULL,
                reason TEXT NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_market_gaps_asset_time ON market_connection_gaps(asset_id, received_ts_ns)")

    def append(self, payload, received_ts_ns=None):
        if not isinstance(payload, dict):
            raise MarketEventError("market event must be a JSON object")
        event_type = str(payload.get("event_type") or "").strip()
        if not event_type:
            raise MarketEventError("event_type is required")
        exchange_ts_ms = _timestamp_ms(payload.get("timestamp"))
        changes = payload.get("price_changes") or payload.get("changes") or []
        change_assets = {str(item.get("asset_id")) for item in changes
                         if isinstance(item, dict) and item.get("asset_id")}
        # A batched price_change can contain multiple tokens. Store it once
        # under the empty asset key and let replay inspect each change.
        inferred_asset = next(iter(change_assets)) if len(change_assets) == 1 else ""
        asset_id = str(payload.get("asset_id") or inferred_asset or "")
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

    def record_connection_gap(self, asset_ids, *, reason="websocket disconnected", received_ts_ns=None):
        """Record a feed interruption for each subscribed token."""
        received = int(received_ts_ns if received_ts_ns is not None else time.time_ns())
        assets = sorted({str(asset).strip() for asset in asset_ids if str(asset).strip()})
        if not assets:
            raise MarketEventError("at least one asset_id is required for a connection gap")
        with self._connect() as db:
            db.executemany(
                "INSERT INTO market_connection_gaps(asset_id,received_ts_ns,reason) VALUES(?,?,?)",
                [(asset, received, str(reason)[:500]) for asset in assets],
            )
        return {"asset_ids": assets, "received_ts_ns": received, "reason": str(reason)[:500]}

    def replay_book(self, asset_id, *, as_of_ms=None, received_before_ns=None):
        """Rebuild a book using only events received by the requested decision time."""
        clauses = ["(asset_id=? OR asset_id='')"]
        args = [str(asset_id)]
        if as_of_ms is not None:
            clauses.append("exchange_ts_ms<=?")
            args.append(int(as_of_ms))
        if received_before_ns is not None:
            clauses.append("received_ts_ns<=?")
            args.append(int(received_before_ns))
        query = ("SELECT * FROM market_events WHERE " + " AND ".join(clauses)
                 + " ORDER BY ingest_seq ASC")
        with self._connect() as db:
            rows = db.execute(query, args).fetchall()
            gap_query = "SELECT * FROM market_connection_gaps WHERE asset_id=?"
            gap_args = [str(asset_id)]
            if received_before_ns is not None:
                gap_query += " AND received_ts_ns<=?"
                gap_args.append(int(received_before_ns))
            gaps = db.execute(gap_query, gap_args).fetchall()

        # Merge events and connection gaps by local arrival time. A disconnect
        # invalidates the current book until a fresh full snapshot is received.
        timeline = [(int(row["received_ts_ns"]), 1, int(row["ingest_seq"]), "event", row)
                    for row in rows]
        timeline += [(int(gap["received_ts_ns"]), 0, int(gap["gap_id"]), "gap", gap)
                     for gap in gaps]
        timeline.sort(key=lambda item: (item[0], item[1], item[2]))

        bids, asks = {}, {}
        has_snapshot = False
        last_exchange_ts = None
        out_of_order = False
        applied = 0
        latest_snapshot_received_ns = None
        for received_ns, _, _, kind, row in timeline:
            if kind == "gap":
                bids, asks = {}, {}
                has_snapshot = False
                latest_snapshot_received_ns = None
                applied += 1
                continue
            payload = json.loads(row["payload_json"])
            event_type = row["event_type"]
            stamp = int(row["exchange_ts_ms"])
            if as_of_ms is not None and stamp > int(as_of_ms):
                continue
            if last_exchange_ts is not None and stamp < last_exchange_ts:
                # Deltas are state transitions. Applying a late older delta
                # after a newer one would corrupt the reconstructed book.
                out_of_order = True
                has_snapshot = False
                continue
            if event_type == "book":
                bids = _levels(payload.get("bids") or payload.get("buys") or [])
                asks = _levels(payload.get("asks") or payload.get("sells") or [])
                has_snapshot = True
                latest_snapshot_received_ns = received_ns
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
            "latest_snapshot_received_ns": latest_snapshot_received_ns,
            "applied_events": applied,
            "last_exchange_ts_ms": last_exchange_ts,
        }

    def count(self):
        with self._connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM market_events").fetchone()[0])
