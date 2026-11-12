"""
SQLite trade persistence + timing fields for temporal edge tuning.
"""
import sqlite3
from pathlib import Path
from datetime import datetime, timezone, date
from typing import Dict, List, Optional
import logging

logger = logging.getLogger("astf.persist")

DEFAULT_DB = Path("trades.db")

TIMING_COLS = [
    ("asset", "TEXT"),
    ("window", "TEXT"),
    ("hour_utc", "INTEGER"),
    ("weekday_utc", "INTEGER"),
    ("time_remaining_s", "REAL"),
    ("time_remaining_pct", "REAL"),
    ("edge_after_fees", "REAL"),
    ("momentum", "REAL"),
    ("regime", "TEXT"),
    ("trade_type", "TEXT"),
    ("hold_seconds", "REAL"),
    ("win", "INTEGER"),
    ("opened_at", "TEXT"),
]


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
            # Migrate older DBs
            existing = {
                r[1]
                for r in conn.execute("PRAGMA table_info(trades)").fetchall()
            }
            for col, typ in TIMING_COLS:
                if col not in existing:
                    conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {typ}")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(timestamp)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trades_hour ON trades(hour_utc)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trades_wd ON trades(weekday_utc)"
            )
            conn.commit()

    def log(self, trade: Dict, score: float = 0.0):
        ts = trade.get("closed_at") or datetime.now(timezone.utc).isoformat()
        sc = float(score if score is not None else trade.get("score") or 0)
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
            sc,
            str(trade.get("asset") or ""),
            str(trade.get("window") or ""),
            int(trade.get("hour_utc") if trade.get("hour_utc") is not None else -1),
            int(trade.get("weekday_utc") if trade.get("weekday_utc") is not None else -1),
            float(trade.get("time_remaining_s") or 0),
            float(trade.get("time_remaining_pct") or 0),
            float(trade.get("edge_after_fees") or 0),
            float(trade.get("momentum") or 0),
            str(trade.get("regime") or ""),
            str(trade.get("trade_type") or ""),
            float(trade.get("hold_seconds") or 0),
            int(trade.get("win") if trade.get("win") is not None else (1 if float(trade.get("pnl") or 0) > 0 else 0)),
            str(trade.get("opened_at") or ""),
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO trades (
                    timestamp, order_id, market_id, slug, direction,
                    entry_price, resolution, shares, size_usd, fee, pnl, score,
                    asset, window, hour_utc, weekday_utc,
                    time_remaining_s, time_remaining_pct,
                    edge_after_fees, momentum, regime, trade_type,
                    hold_seconds, win, opened_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
            conn.commit()
        logger.debug(
            f"SQLite logged {row[1]} pnl={row[10]:.4f} h={row[14]} wd={row[15]} win={row[13]}"
        )

    def daily_summary(self, day: Optional[date] = None) -> Dict:
        day = day or datetime.now(timezone.utc).date()
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

    def timing_summary(self, min_trades: int = 5) -> Dict:
        """
        Aggregate win rate / pnl by hour_utc and weekday_utc for future filters.
        """
        with self._connect() as conn:
            by_hour = conn.execute(
                """
                SELECT hour_utc as k,
                       COUNT(*) as n,
                       COALESCE(SUM(win), 0) as wins,
                       COALESCE(SUM(pnl), 0) as pnl
                FROM trades
                WHERE hour_utc IS NOT NULL AND hour_utc >= 0
                GROUP BY hour_utc
                ORDER BY hour_utc
                """
            ).fetchall()
            by_wd = conn.execute(
                """
                SELECT weekday_utc as k,
                       COUNT(*) as n,
                       COALESCE(SUM(win), 0) as wins,
                       COALESCE(SUM(pnl), 0) as pnl
                FROM trades
                WHERE weekday_utc IS NOT NULL AND weekday_utc >= 0
                GROUP BY weekday_utc
                ORDER BY weekday_utc
                """
            ).fetchall()
            by_window = conn.execute(
                """
                SELECT window as k,
                       COUNT(*) as n,
                       COALESCE(SUM(win), 0) as wins,
                       COALESCE(SUM(pnl), 0) as pnl
                FROM trades
                WHERE window IS NOT NULL AND window != ''
                GROUP BY window
                """
            ).fetchall()

        def pack(rows):
            out = {}
            for r in rows:
                n = int(r["n"] or 0)
                if n < min_trades:
                    continue
                wins = int(r["wins"] or 0)
                out[str(r["k"])] = {
                    "n": n,
                    "wins": wins,
                    "win_rate": wins / n if n else 0.0,
                    "pnl": float(r["pnl"] or 0),
                }
            return out

        return {
            "by_hour_utc": pack(by_hour),
            "by_weekday_utc": pack(by_wd),
            "by_window": pack(by_window),
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
