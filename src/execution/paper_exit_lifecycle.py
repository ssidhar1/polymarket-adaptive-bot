"""Durable paper-only entry/exit fill journal and realized-P&L ledger.

CLOB V2 fees are modeled as USDC cash costs by default. An explicit legacy
"shares" mode remains available for V1-era fills. The ledger normalizes both
modes into net shares and fee-adjusted cost basis for correct P&L.
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
                fee_shares REAL NOT NULL DEFAULT 0 CHECK(fee_shares >= 0),
                fee_collection TEXT NOT NULL DEFAULT 'usdc' CHECK(fee_collection IN ('usdc','shares')),
                gross_pnl_usd REAL, entry_fee_allocated_usd REAL, net_pnl_usd REAL,
                created_at TEXT NOT NULL)""")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(paper_fill_events)")}
            if "fee_shares" not in columns:
                db.execute("ALTER TABLE paper_fill_events ADD COLUMN fee_shares REAL NOT NULL DEFAULT 0")
            if "fee_collection" not in columns:
                db.execute("ALTER TABLE paper_fill_events ADD COLUMN fee_collection TEXT NOT NULL DEFAULT 'usdc'")
                # Interim paper builds wrote explicit fee_shares before the
                # collection mode existed. Preserve those as legacy V1 fills;
                # all older rows remain V2-style USDC fees.
                db.execute("""UPDATE paper_fill_events SET fee_collection='shares'
                    WHERE side='BUY' AND fee_shares > 0""")
            db.execute("""CREATE TABLE IF NOT EXISTS paper_settlement_events (
                event_id TEXT PRIMARY KEY,
                position_id TEXT NOT NULL REFERENCES paper_positions(position_id),
                quantity REAL NOT NULL CHECK(quantity > 0),
                resolution_price REAL NOT NULL CHECK(resolution_price >= 0 AND resolution_price <= 1),
                gross_pnl_usd REAL NOT NULL, entry_fee_allocated_usd REAL NOT NULL,
                net_pnl_usd REAL NOT NULL, created_at TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_paper_events_position ON paper_fill_events(position_id,side)")
            self._reconcile_fee_share_accounting(db)

    @staticmethod
    def _reconcile_fee_share_accounting(db):
        """Reprice historical realized rows after BUY-fee shares are recognized."""
        positions = [row[0] for row in db.execute("SELECT position_id FROM paper_positions")]
        for position_id in positions:
            buys = db.execute("""SELECT COALESCE(SUM(quantity),0) gross_q,
                COALESCE(SUM(quantity*price),0) cost,COALESCE(SUM(fee_usd),0) fees,
                COALESCE(SUM(CASE WHEN fee_collection='usdc' THEN fee_usd ELSE 0 END),0) cash_fees,
                COALESCE(SUM(fee_shares),0) fee_shares
                FROM paper_fill_events WHERE position_id=? AND side='BUY'""",
                (position_id,)).fetchone()
            net_q = float(buys["gross_q"]) - float(buys["fee_shares"])
            if net_q <= 1e-12:
                continue
            avg = (float(buys["cost"]) + float(buys["cash_fees"])) / net_q
            entry_fees = float(buys["fees"])
            sells = db.execute("""SELECT event_id,quantity,price,fee_usd
                FROM paper_fill_events WHERE position_id=? AND side='SELL'""",
                (position_id,)).fetchall()
            for sell in sells:
                qty = float(sell["quantity"])
                gross = (float(sell["price"]) - avg) * qty
                allocated = entry_fees * qty / net_q
                net = gross - float(sell["fee_usd"])
                db.execute("""UPDATE paper_fill_events
                    SET gross_pnl_usd=?,entry_fee_allocated_usd=?,net_pnl_usd=?
                    WHERE event_id=?""", (gross, allocated, net, sell["event_id"]))
            settlements = db.execute("""SELECT event_id,quantity,resolution_price
                FROM paper_settlement_events WHERE position_id=?""",
                (position_id,)).fetchall()
            for settlement in settlements:
                qty = float(settlement["quantity"])
                gross = (float(settlement["resolution_price"]) - avg) * qty
                allocated = entry_fees * qty / net_q
                db.execute("""UPDATE paper_settlement_events
                    SET gross_pnl_usd=?,entry_fee_allocated_usd=?,net_pnl_usd=?
                    WHERE event_id=?""", (gross, allocated, gross, settlement["event_id"]))

    def record_entry_fill(self, *, position_id, token_id, market_id, direction,
                          quantity, price, fee_usd, fee_shares=None,
                          fee_collection="usdc", opened_at=None,
                          metadata=None, event_id=None):
        q, p, fee = float(quantity), float(price), float(fee_usd)
        collection = str(fee_collection or "usdc").lower()
        if collection not in ("usdc", "shares"):
            raise PaperLifecycleError("invalid fee collection mode")
        withheld = (
            (fee / p if p > 0 else 0.0) if fee_shares is None and collection == "shares"
            else float(fee_shares or 0.0)
        )
        if (not position_id or not token_id or not market_id or q <= 0
                or not 0 < p < 1 or fee < 0 or withheld < 0 or withheld >= q
                or (collection == "usdc" and withheld > 0)):
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
                (event_id,position_id,side,quantity,price,fee_usd,fee_shares,fee_collection,created_at)
                VALUES(?,?, 'BUY',?,?,?,?,?,?)""",
                (str(eid), str(position_id), q, p, fee, withheld, collection, str(stamp)))
            row = db.execute("""SELECT position_id,quantity,price,fee_usd,fee_shares,fee_collection
                FROM paper_fill_events WHERE event_id=?""", (str(eid),)).fetchone()
            if (row["position_id"] != str(position_id) or row["quantity"] != q
                    or row["price"] != p or row["fee_usd"] != fee
                    or row["fee_shares"] != withheld or row["fee_collection"] != collection):
                raise PaperLifecycleError("entry event id reused with different fill")
        return self.get_position(position_id)

    def get_position(self, position_id):
        with self._connect() as db:
            pos = db.execute("SELECT * FROM paper_positions WHERE position_id=?", (str(position_id),)).fetchone()
            if not pos:
                return None
            buys = db.execute("""SELECT COALESCE(SUM(quantity),0) q,
                COALESCE(SUM(quantity*price),0) cost,COALESCE(SUM(fee_usd),0) fees,
                COALESCE(SUM(CASE WHEN fee_collection='usdc' THEN fee_usd ELSE 0 END),0) cash_fees,
                COALESCE(SUM(fee_shares),0) fee_shares
                FROM paper_fill_events WHERE position_id=? AND side='BUY'""", (str(position_id),)).fetchone()
            sells = db.execute("""SELECT COALESCE(SUM(quantity),0) q,
                COALESCE(SUM(net_pnl_usd),0) net,COALESCE(SUM(gross_pnl_usd),0) gross,
                COALESCE(SUM(fee_usd),0) fees,COALESCE(SUM(entry_fee_allocated_usd),0) entry_fees
                FROM paper_fill_events WHERE position_id=? AND side='SELL'""", (str(position_id),)).fetchone()
            settles = db.execute("""SELECT COALESCE(SUM(quantity),0) q,
                COALESCE(SUM(net_pnl_usd),0) net,COALESCE(SUM(gross_pnl_usd),0) gross,
                COALESCE(SUM(entry_fee_allocated_usd),0) entry_fees
                FROM paper_settlement_events WHERE position_id=?""", (str(position_id),)).fetchone()
            gross_entry_qty = float(buys["q"])
            entry_fee_shares = float(buys["fee_shares"])
            entry_qty = max(0.0, gross_entry_qty - entry_fee_shares)
            exit_qty = float(sells["q"]) + float(settles["q"])
            remaining = max(0.0, entry_qty - exit_qty)
            avg = (float(buys["cost"]) + float(buys["cash_fees"])) / entry_qty if entry_qty else 0.0
            return {
                "position_id": str(position_id), "token_id": pos["token_id"],
                "market_id": pos["market_id"], "direction": pos["direction"],
                "opened_at": pos["opened_at"], "metadata": json.loads(pos["metadata_json"]),
                "entry_quantity": entry_qty, "gross_entry_quantity": gross_entry_qty,
                "entry_fee_shares": entry_fee_shares, "exit_quantity": exit_qty,
                "remaining_quantity": remaining, "average_entry_price": avg,
                "entry_fees_usd": float(buys["fees"]),
                "entry_cash_fees_usd": float(buys["cash_fees"]),
                "realized_gross_pnl_usd": float(sells["gross"]) + float(settles["gross"]),
                "realized_net_pnl_usd": float(sells["net"]) + float(settles["net"]),
                "exit_fees_usd": float(sells["fees"]),
                # Audit allocation only: entry fees are already reflected in
                # the higher cost basis per net share, so never subtract twice.
                "allocated_entry_fees_usd": float(sells["entry_fees"]) + float(settles["entry_fees"]),
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
                if (prior["position_id"] != str(position_id) or prior["side"] != "SELL"
                        or prior["quantity"] != q or prior["price"] != p or prior["fee_usd"] != fee):
                    raise PaperLifecycleError("exit event id reused with different fill")
                return self.get_position(position_id)
            buys = db.execute("""SELECT COALESCE(SUM(quantity),0) q,
                COALESCE(SUM(quantity*price),0) cost,COALESCE(SUM(fee_usd),0) fees,
                COALESCE(SUM(CASE WHEN fee_collection='usdc' THEN fee_usd ELSE 0 END),0) cash_fees,
                COALESCE(SUM(fee_shares),0) fee_shares
                FROM paper_fill_events WHERE position_id=? AND side='BUY'""", (str(position_id),)).fetchone()
            exits = db.execute("""SELECT COALESCE(SUM(quantity),0) q FROM paper_fill_events
                WHERE position_id=? AND side='SELL'""", (str(position_id),)).fetchone()
            settled = db.execute("SELECT COALESCE(SUM(quantity),0) q FROM paper_settlement_events WHERE position_id=?",
                                 (str(position_id),)).fetchone()
            total_q = float(buys["q"]) - float(buys["fee_shares"])
            remaining = total_q - float(exits["q"]) - float(settled["q"])
            if q > remaining + 1e-9 or total_q <= 0:
                raise PaperLifecycleError("exit quantity exceeds remaining paper position")
            avg = (float(buys["cost"]) + float(buys["cash_fees"])) / total_q
            entry_fee_alloc = float(buys["fees"]) * q / total_q
            gross = (p - avg) * q
            # Entry fees are included in the fee-adjusted average basis.
            # Only the SELL-side USDC fee is deducted here.
            net = gross - fee
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
                COALESCE(SUM(quantity*price),0) cost,COALESCE(SUM(fee_usd),0) fees,
                COALESCE(SUM(CASE WHEN fee_collection='usdc' THEN fee_usd ELSE 0 END),0) cash_fees,
                COALESCE(SUM(fee_shares),0) fee_shares
                FROM paper_fill_events WHERE position_id=? AND side='BUY'""",
                (str(position_id),)).fetchone()
            exits = db.execute("SELECT COALESCE(SUM(quantity),0) q FROM paper_fill_events WHERE position_id=? AND side='SELL'",
                               (str(position_id),)).fetchone()
            settled = db.execute("SELECT COALESCE(SUM(quantity),0) q FROM paper_settlement_events WHERE position_id=?",
                                 (str(position_id),)).fetchone()
            total_q = float(buys["q"]) - float(buys["fee_shares"])
            remaining = total_q - float(exits["q"]) - float(settled["q"])
            if total_q <= 0 or remaining <= 1e-9:
                raise PaperLifecycleError("no remaining shares to settle")
            avg = (float(buys["cost"]) + float(buys["cash_fees"])) / total_q
            allocated = float(buys["fees"]) * remaining / total_q
            gross = (price - avg) * remaining
            net = gross
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
                COALESCE(SUM(CASE WHEN side='BUY' THEN quantity-fee_shares ELSE 0 END),0) bought,
                COALESCE(SUM(CASE WHEN side='BUY' THEN quantity ELSE 0 END),0) gross_bought,
                COALESCE(SUM(CASE WHEN side='BUY' THEN fee_shares ELSE 0 END),0) fee_shares,
                COALESCE(SUM(CASE WHEN side='SELL' THEN quantity ELSE 0 END),0) sold,
                COALESCE(SUM(CASE WHEN side='SELL' THEN gross_pnl_usd ELSE 0 END),0) gross_pnl,
                COALESCE(SUM(CASE WHEN side='SELL' THEN net_pnl_usd ELSE 0 END),0) net_pnl,
                COALESCE(SUM(fee_usd),0) fees FROM paper_fill_events""").fetchone()
            settle = db.execute("""SELECT COALESCE(SUM(quantity),0) q,
                COALESCE(SUM(gross_pnl_usd),0) gross,COALESCE(SUM(net_pnl_usd),0) net
                FROM paper_settlement_events""").fetchone()
            today = datetime.now(timezone.utc).date().isoformat()
            daily_sells = db.execute(
                """SELECT COALESCE(SUM(net_pnl_usd),0) FROM paper_fill_events
                   WHERE side='SELL' AND substr(created_at,1,10)=?""", (today,)
            ).fetchone()[0]
            daily_settlements = db.execute(
                """SELECT COALESCE(SUM(net_pnl_usd),0) FROM paper_settlement_events
                   WHERE substr(created_at,1,10)=?""", (today,)
            ).fetchone()[0]
        open_count = len(self.open_positions())
        return {
            "fill_events": int(row["fills"]), "entry_shares": float(row["bought"]),
            "gross_entry_shares": float(row["gross_bought"]),
            "entry_fee_shares": float(row["fee_shares"]),
            "exit_shares": float(row["sold"]) + float(settle["q"]),
            "open_positions": int(open_count),
            "gross_realized_pnl_usd": float(row["gross_pnl"]) + float(settle["gross"]),
            "net_realized_pnl_usd": float(row["net_pnl"]) + float(settle["net"]),
            "daily_realized_net_pnl_usd": float(daily_sells) + float(daily_settlements),
            "daily_realized_day_utc": today,
            "fees_usd": float(row["fees"]),
            "pnl_scope": "paper simulation; configured fee/slippage model, not exchange settlement data",
        }
