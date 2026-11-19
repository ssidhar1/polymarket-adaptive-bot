"""Durable paper-only entry/exit fill journal and realized-P&L ledger.

The SQLite event log is the source of truth for partial fills across restarts.
It simulates fills; it does not connect to or submit exchange orders.
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class PaperLifecycleError(ValueError):
    pass


class PaperExitLifecycle:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self):
        db = sqlite3.connect(str(self.path), timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _init_db(self):
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS paper_positions (
                position_id TEXT PRIMARY KEY,
                token_id TEXT NOT NULL,
                market_id TEXT NOT NULL,
                direction TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS paper_fill_events (
                event_id TEXT PRIMARY KEY,
                position_id TEXT NOT NULL REFERENCES paper_positions(position_id),
                side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
                quantity REAL NOT NULL CHECK(quantity > 0),
                price REAL NOT NULL CHECK(price > 0 AND price < 1),
                fee_usd REAL NOT NULL CHECK(fee_usd >= 0),
                gross_pnl_usd REAL,
                entry_fee_allocated_usd REAL,
                net_pnl_usd REAL,
                created_at TEXT NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_paper_events_position ON paper_fill_events(position_id,side)")

    def record_entry_fill(self, *, position_id, token_id, market_id, direction,
                          quantity, price, fee_usd, opened_at=None, metadata=None,
                          event_id=None):
        q, p, fee = float(quantity), float(price), float(fee_usd)
        if not position_id or not token_id or not market_id or q <= 0 or not 0 < p < 1 or fee < 0:
            raise PaperLifecycleError("invalid paper entry fill")
        stamp = opened_at or datetime.now(timezone.utc).isoformat()
        eid = event_id or f"entry:{position_id}:{stamp}:{q}:{p}"
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT OR IGNORE INTO paper_positions
                (position_id,token_id,market_id,direction,opened_at,metadata_json)
                VALUES(?,?,?,?,?,?)""",
                (str(position_id), str(token_id), str(market_id), str(direction),
                 str(stamp), json.dumps(metadata or {}, sort_keys=True)))
            pos = db.execute("SELECT token_id,market_id,direction FROM paper_positions WHERE position_id=?",
                             (str(position_id),)).fetchone()
            if not pos or pos["token_id"] != str(token_id) or pos["market_id"] != str(market_id):
                raise PaperLifecycleError("position identity mismatch")
            db.execute("""INSERT OR IGNORE INTO paper_fill_events
                (event_id,position_id,side,quantity,price,fee_usd,created_at)
                VALUES(?,?, 'BUY',?,?,?,?)""",
                (str(eid), str(position_id), q, p, fee, str(stamp)))
            row = db.execute("SELECT position_id,quantity,price,fee_usd FROM paper_fill_events WHERE event_id=?",
                             (str(eid),)).fetchone()
            if row["position_id"] != str(position_id) or row["quantity"] != q or row["price"] != p:
                raise PaperLifecycleError("entry event id reused with different fill")
        return self.get_position(position_id)

    def get_position(self, position_id):
        with self._connect() as db:
            pos = db.execute("SELECT * FROM paper_positions WHERE position_id=?", (str(position_id),)).fetchone()
            if not pos:
                return None
            buys = db.execute("""SELECT COALESCE(SUM(quantity),0) q,
                COALESCE(SUM(quantity*price),0) cost,COALESCE(SUM(fee_usd),0) fees
                FROM paper_fill_events WHERE position_id=? AND side='BUY'""", (str(position_id),)).fetchone()
            sells = db.execute("""SELECT COALESCE(SUM(quantity),0) q,
                COALESCE(SUM(net_pnl_usd),0) net,COALESCE(SUM(gross_pnl_usd),0) gross,
                COALESCE(SUM(fee_usd),0) fees,COALESCE(SUM(entry_fee_allocated_usd),0) entry_fees
                FROM paper_fill_events WHERE position_id=? AND side='SELL'""", (str(position_id),)).fetchone()
            entry_qty = float(buys["q"])
            exit_qty = float(sells["q"])
            remaining = max(0.0, entry_qty-exit_qty)
            avg = float(buys["cost"])/entry_qty if entry_qty else 0.0
            return {
                "position_id": str(position_id), "token_id": pos["token_id"],
                "market_id": pos["market_id"], "direction": pos["direction"],
                "opened_at": pos["opened_at"], "metadata": json.loads(pos["metadata_json"]),
                "entry_quantity": entry_qty, "exit_quantity": exit_qty,
                "remaining_quantity": remaining, "average_entry_price": avg,
                "entry_fees_usd": float(buys["fees"]),
                "realized_gross_pnl_usd": float(sells["gross"]),
                "realized_net_pnl_usd": float(sells["net"]),
                "exit_fees_usd": float(sells["fees"]),
                "allocated_entry_fees_usd": float(sells["entry_fees"]),
                "is_open": remaining > 1e-9,
            }

    def open_positions(self):
        with self._connect() as db:
            ids = [r[0] for r in db.execute("SELECT position_id FROM paper_positions ORDER BY opened_at")]
        return [p for pid in ids if (p := self.get_position(pid)) and p["is_open"]]

    def record_exit_fill(self, *, position_id, event_id, quantity, price, fee_usd):
        q, p, fee = float(quantity), float(price), float(fee_usd)
        if not event_id or q <= 0 or not 0 < p < 1 or fee < 0:
            raise PaperLifecycleError("invalid paper exit fill")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM paper_fill_events WHERE event_id=?", (str(event_id),)).fetchone()
            if prior:
                if prior["position_id"] != str(position_id) or prior["side"] != "SELL" or prior["quantity"] != q or prior["price"] != p:
                    raise PaperLifecycleError("exit event id reused with different fill")
                return self.get_position(position_id)
            buys = db.execute("""SELECT COALESCE(SUM(quantity),0) q,
                COALESCE(SUM(quantity*price),0) cost,COALESCE(SUM(fee_usd),0) fees
                FROM paper_fill_events WHERE position_id=? AND side='BUY'""", (str(position_id),)).fetchone()
            sells = db.execute("SELECT COALESCE(SUM(quantity),0) q FROM paper_fill_events WHERE position_id=? AND side='SELL'",
                               (str(position_id),)).fetchone()
            total_q = float(buys["q"])
            remaining = total_q-float(sells["q"])
            if q > remaining + 1e-9 or total_q <= 0:
                raise PaperLifecycleError("exit quantity exceeds remaining paper position")
            avg = float(buys["cost"])/total_q
            entry_fee_alloc = float(buys["fees"]) * q / total_q
            gross = (p-avg)*q
            net = gross-entry_fee_alloc-fee
            db.execute("""INSERT INTO paper_fill_events
                (event_id,position_id,side,quantity,price,fee_usd,gross_pnl_usd,
                 entry_fee_allocated_usd,net_pnl_usd,created_at)
                VALUES(?,?,'SELL',?,?,?,?,?,?,?)""",
                (str(event_id), str(position_id), q, p, fee, gross,
                 entry_fee_alloc, net, datetime.now(timezone.utc).isoformat()))
        return self.get_position(position_id)

    def summary(self):
        with self._connect() as db:
            row = db.execute("""SELECT COUNT(*) fills,
                COALESCE(SUM(CASE WHEN side='BUY' THEN quantity ELSE 0 END),0) bought,
                COALESCE(SUM(CASE WHEN side='SELL' THEN quantity ELSE 0 END),0) sold,
                COALESCE(SUM(CASE WHEN side='SELL' THEN gross_pnl_usd ELSE 0 END),0) gross_pnl,
                COALESCE(SUM(CASE WHEN side='SELL' THEN net_pnl_usd ELSE 0 END),0) net_pnl,
                COALESCE(SUM(fee_usd),0) fees FROM paper_fill_events""").fetchone()
            open_count = db.execute("""SELECT COUNT(*) FROM (
                SELECT p.position_id,
                COALESCE(SUM(CASE WHEN e.side='BUY' THEN e.quantity ELSE -e.quantity END),0) q
                FROM paper_positions p LEFT JOIN paper_fill_events e ON p.position_id=e.position_id
                GROUP BY p.position_id HAVING q > 0.000000001)""").fetchone()[0]
        return {"fill_events": int(row["fills"]), "entry_shares": float(row["bought"]),
                "exit_shares": float(row["sold"]), "open_positions": int(open_count),
                "gross_realized_pnl_usd": float(row["gross_pnl"]),
                "net_realized_pnl_usd": float(row["net_pnl"]),
                "fees_usd": float(row["fees"]),
                "pnl_scope": "paper simulation; fee model is configured approximation, not exchange settlement data"}
