"""
SQLite trade persistence + summaries.
Falls back gracefully; also mirrors last N rows conceptually for reports.
"""
import sqlite3
from pathlib import Path
from datetime import datetime, timezone, date
from typing import Dict, List, Optional
import logging

logger = logging.getLogger("astf.persist")

DEFAULT_DB = Path("trades.db")


class TradeLogger:
    def __init__(self, path: Path = DEFAULT_DB):
        self.path = Path(path)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    order_id TEXT,
                    market_id TEXT,
                    slug TEXT,
                    direction TEXT,
                    entry_price REAL,
                    resolution REAL,
                    shares REAL,
                    size_usd REAL,
                    fee REAL,
                    pnl REAL,
                    score REAL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(timestamp)"
            )
            conn.commit()

    def log(self, trade: Dict, score: float = 0.0):
        ts = trade.get("closed_at") or datetime.now(timezone.utc).isoformat()
        row = (
            ts,
            trade.get("order_id", ""),
            trade.get("market_id", ""),
            trade.get("slug", ""),
            trade.get("direction", ""),
            float(trade.get("entry_price", 0) or 0),
            float(trade.get("resolution", 0) or 0),
            float(trade.get("shares", 0) or 0),
            float(trade.get("size_usd", 0) or 0),
            float(trade.get("fee", 0) or 0),
            float(trade.get("pnl", 0) or 0),
            float(score or 0),
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO trades (
                    timestamp, order_id, market_id, slug, direction,
                    entry_price, resolution, shares, size_usd, fee, pnl, score
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
            conn.commit()
        logger.debug(f"SQLite logged {row[1]} pnl={row[10]:.4f}")

    def daily_summary(self, day: Optional[date] = None) -> Dict:
        day = day or date.today()
        prefix = day.isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT COUNT(*) as n,
                       COALESCE(SUM(pnl), 0) as pnl,
                       COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) as wins
                FROM trades
                WHERE timestamp LIKE ?
                """,
                (f"{prefix}%",),
            )
            r = cur.fetchone()
        n = int(r["n"] or 0)
        wins = int(r["wins"] or 0)
        pnl = float(r["pnl"] or 0)
        return {
            "trades": n,
            "pnl": pnl,
            "wins": wins,
            "win_rate": wins / n if n else 0.0,
        }

    def all_time_summary(self) -> Dict:
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT COUNT(*) as n,
                       COALESCE(SUM(pnl), 0) as pnl,
                       COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) as wins,
                       COALESCE(AVG(score), 0) as avg_score
                FROM trades
                """
            )
            r = cur.fetchone()
        n = int(r["n"] or 0)
        wins = int(r["wins"] or 0)
        return {
            "trades": n,
            "pnl": float(r["pnl"] or 0),
            "wins": wins,
            "win_rate": wins / n if n else 0.0,
            "avg_score": float(r["avg_score"] or 0),
        }

    def recent(self, limit: int = 20) -> List[Dict]:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in cur.fetchall()]
