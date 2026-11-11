"""
Persistent trade logging + simple daily summary.
Writes to trades.csv and keeps a lightweight in-memory view.
"""
import csv
from pathlib import Path
from datetime import datetime, timezone, date
from typing import List, Dict
import logging

logger = logging.getLogger("astf.persist")

TRADES_FILE = Path("trades.csv")
HEADERS = [
    "timestamp", "order_id", "market_id", "slug", "direction",
    "entry_price", "resolution", "shares", "size_usd", "fee", "pnl", "score"
]

class TradeLogger:
    def __init__(self, path: Path = TRADES_FILE):
        self.path = path
        self._ensure_file()

    def _ensure_file(self):
        if not self.path.exists():
            with open(self.path, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=HEADERS).writeheader()

    def log(self, trade: Dict, score: float = 0.0):
        row = {
            "timestamp": trade.get("closed_at") or datetime.now(timezone.utc).isoformat(),
            "order_id": trade.get("order_id", ""),
            "market_id": trade.get("market_id", ""),
            "slug": trade.get("slug", ""),
            "direction": trade.get("direction", ""),
            "entry_price": f"{trade.get('entry_price', 0):.4f}",
            "resolution": f"{trade.get('resolution', 0):.4f}",
            "shares": f"{trade.get('shares', 0):.4f}",
            "size_usd": f"{trade.get('size_usd', 0):.2f}",
            "fee": f"{trade.get('fee', 0):.4f}",
            "pnl": f"{trade.get('pnl', 0):.4f}",
            "score": f"{score:.3f}",
        }
        with open(self.path, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=HEADERS).writerow(row)
        logger.debug(f"Logged trade {row['order_id']} pnl={row['pnl']}")

    def daily_summary(self) -> Dict:
        if not self.path.exists():
            return {"trades": 0, "pnl": 0.0, "wins": 0}
        today = date.today().isoformat()
        trades = 0
        pnl = 0.0
        wins = 0
        with open(self.path) as f:
            for row in csv.DictReader(f):
                if row["timestamp"].startswith(today):
                    trades += 1
                    p = float(row["pnl"])
                    pnl += p
                    if p > 0:
                        wins += 1
        return {
            "trades": trades,
            "pnl": pnl,
            "wins": wins,
            "win_rate": wins / trades if trades else 0.0,
        }
