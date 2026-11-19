"""Durable paper-only entry/exit fill journal and realized-P&L ledger."""
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
                position_id TEXT PRIMARY KEY, token_id TEXT NOT NULL,
                market_id TEXT NOT NULL, direction TEXT NOT NULL,
                opened_at TEXT NOT NULL, metadata_json TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS paper_fill_events (
                event_id TEXT PRIMARY KEY,
                position_id TEXT NOT NULL REFERENCES paper_positions(position_id),
                side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
                quantity REAL NOT NULL CHECK(quantity > 0),
                price REAL NOT NULL CHECK(price > 0 AND price < 1),
                fee_usd REAL NOT NULL CHECK(fee_usd >= 0),
                gross_pnl_usd REAL, entry_fee_allocated_usd REAL, net_pnl_usd REAL,
                created_at TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS paper_settlement_events (
                event_id TEXT PRIMARY KEY,
                position_id TEXT NOT NULL REFERENCES paper_positions(position_id),
                quantity REAL NOT NULL CHECK(quantity > 0),
                resolution_price REAL NOT NULL CHECK(resolution_price >= 0 AND resolution_price <= 1),
                gross_pnl_usd REAL NOT NULL, entry_fee_allocated_usd REAL NOT NULL,
                net_pnl_usd REAL NOT NULL, created_at TEXT NOT NULL)""")
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
            if row["position_id"] != str(position_id) or row["quantity"] != q or row["price"] != p or row["fee_usd"] != fee:
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
            settles = db.execute("""SELECT COALESCE(SUM(quantity),0) q,
                COALESCE(SUM(net_pnl_usd),0) net,COALESCE(SUM(gross_pnl_usd),0) gross,
                COALESCE(SUM(entry_fee_allocated_usd),0) entry_fees
                FROM paper_settlement_events WHERE position_id=?""", (str(position_id),)).fetchone()
            entry_qty = float(buys["q"])
            exit_qty = float(sells["q"]) + float(settles["q"])
            remaining = max(0.0, entry_qty-exit_qty)
            avg = float(buys["cost"])/entry_qty if entry_qty else 0.0
            return {
                "position_id": str(position_id), "token_id": pos["token_id"],
                "market_id": pos["market_id"], "direction": pos["direction"],
                "opened_at": pos["opened_at"], "metadata": json.loads(pos["metadata_json"]),
                "entry_quantity": entry_qty, "exit_quantity": exit_qty,
                "remaining_quantity": remaining, "average_entry_price": avg,
                "entry_fees_usd": float(buys["fees"]),
                "realized_gross_pnl_usd": float(sells["gross"])+float(settles["gross"]),
                "realized_net_pnl_usd": float(sells["net"])+float(settles["net"]),
                "exit_fees_usd": float(sells["fees"]),
                "allocated_entry_fees_usd": float(sells["entry_fees"])+float(settles["entry_fees"]),
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
                if prior["position_id"] != str(position_id) or prior["side"] != "SELL" or prior["quantity"] != q or prior["price"] != p or prior["fee_usd"] != fee:
                    raise PaperLifecycleError("exit event id reused with different fill")
                return self.get_position(position_id)
            buys = db.execute("""SELECT COALESCE(SUM(quantity),0) q,
                COALESCE(SUM(quantity*price),0) cost,COALESCE(SUM(fee_usd),0) fees
                FROM paper_fill_events WHERE position_id=? AND side='BUY'""", (str(position_id),)).fetchone()
            exits = db.execute("""SELECT COALESCE(SUM(quantity),0) q FROM paper_fill_events
                WHERE position_id=? AND side='SELL'""", (str(position_id),)).fetchone()
            settled = db.execute("SELECT COALESCE(SUM(quantity),0) q FROM paper_settlement_events WHERE position_id=?",
                                 (str(position_id),)).fetchone()
            total_q = float(buys["q"])
            remaining = total_q-float(exits["q"])-float(settled["q"])
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

    def record_settlement(self, *, position_id, event_id, resolution_price):
        price = float(resolution_price)
        if not event_id or not 0 <= price <= 1:
            raise PaperLifecycleError("invalid paper settlement")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM paper_settlement_events WHERE event_id=?",
                               (str(event_id),)).fetchone()
            if prior:
                if prior["position_id"] != str(position_id) or prior["resolution_price"] != price:
                    raise PaperLifecycleError("settlement event id reused with different outcome")
                return self.get_position(position_id)
            buys = db.execute("""SELECT COALESCE(SUM(quantity),0) q,
                COALESCE(SUM(quantity*price),0) cost,COALESCE(SUM(fee_usd),0) fees
                FROM paper_fill_events WHERE position_id=? AND side='BUY'""",
                (str(position_id),)).fetchone()
            exits = db.execute("SELECT COALESCE(SUM(quantity),0) q FROM paper_fill_events WHERE position_id=? AND side='SELL'",
                               (str(position_id),)).fetchone()
            settled = db.execute("SELECT COALESCE(SUM(quantity),0) q FROM paper_settlement_events WHERE position_id=?",
                                 (str(position_id),)).fetchone()
            total_q = float(buys["q"])
            remaining = total_q-float(exits["q"])-float(settled["q"])
            if total_q <= 0 or remaining <= 1e-9:
                raise PaperLifecycleError("no remaining shares to settle")
            avg = float(buys["cost"])/total_q
            allocated = float(buys["fees"]) * remaining / total_q
            gross = (price-avg)*remaining
            net = gross-allocated
            db.execute("""INSERT INTO paper_settlement_events
                (event_id,position_id,quantity,resolution_price,gross_pnl_usd,
                 entry_fee_allocated_usd,net_pnl_usd,created_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (str(event_id), str(position_id), remaining, price, gross, allocated,
                 net, datetime.now(timezone.utc).isoformat()))
        return self.get_position(position_id)

    def summary(self):
        with self._connect() as db:
            row = db.execute("""SELECT COUNT(*) fills,
                COALESCE(SUM(CASE WHEN side='BUY' THEN quantity ELSE 0 END),0) bought,
                COALESCE(SUM(CASE WHEN side='SELL' THEN quantity ELSE 0 END),0) sold,
                COALESCE(SUM(CASE WHEN side='SELL' THEN gross_pnl_usd ELSE 0 END),0) gross_pnl,
                COALESCE(SUM(CASE WHEN side='SELL' THEN net_pnl_usd ELSE 0 END),0) net_pnl,
                COALESCE(SUM(fee_usd),0) fees FROM paper_fill_events""").fetchone()
            settle = db.execute("""SELECT COALESCE(SUM(quantity),0) q,
                COALESCE(SUM(gross_pnl_usd),0) gross,COALESCE(SUM(net_pnl_usd),0) net
                FROM paper_settlement_events""").fetchone()
        open_count = len(self.open_positions())
        return {"fill_events": int(row["fills"]), "entry_shares": float(row["bought"]),
                "exit_shares": float(row["sold"])+float(settle["q"]), "open_positions": int(open_count),
                "gross_realized_pnl_usd": float(row["gross_pnl"])+float(settle["gross"]),
                "net_realized_pnl_usd": float(row["net_pnl"])+float(settle["net"]),
                "fees_usd": float(row["fees"]),
                "pnl_scope": "paper simulation; configured fee/slippage model, not exchange settlement data"}
