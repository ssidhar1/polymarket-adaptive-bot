"""
Position tracker + resolution logic for paper (and later live) trades.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from datetime import datetime, timezone
import logging

logger = logging.getLogger("astf.positions")

@dataclass
class OpenPosition:
    order_id: str
    market_id: str
    token_id: str
    direction: str          # "up" or "down"
    side: str               # "buy"
    entry_price: float
    size_usd: float
    shares: float
    fee: float
    opened_at: datetime
    slug: str = ""
    end_date: Optional[str] = None

class PositionTracker:
    def __init__(self):
        self.open: Dict[str, OpenPosition] = {}
        self.closed: List[Dict] = []

    def add(self, order: Dict, direction: str, slug: str = "", end_date: str = None):
        pos = OpenPosition(
            order_id=order["order_id"],
            market_id=order.get("market_id", ""),
            token_id=order["token_id"],
            direction=direction,
            side=order["side"],
            entry_price=order["fill_price"],
            size_usd=order["size_usd"],
            shares=order["shares"],
            fee=order["fee"],
            opened_at=datetime.now(timezone.utc),
            slug=slug,
            end_date=end_date,
        )
        self.open[order["order_id"]] = pos
        logger.info(f"Opened position {order['order_id']} {direction} @ {order['fill_price']:.3f}")

    def resolve(self, order_id: str, resolution_price: float) -> Optional[Dict]:
        """
        resolution_price should be 1.0 if the side we bought won, else 0.0
        (or the actual final price of the token we hold).
        """
        if order_id not in self.open:
            return None
        pos = self.open.pop(order_id)
        pnl = (resolution_price - pos.entry_price) * pos.shares - pos.fee
        record = {
            "order_id": order_id,
            "market_id": pos.market_id,
            "direction": pos.direction,
            "entry_price": pos.entry_price,
            "resolution": resolution_price,
            "shares": pos.shares,
            "size_usd": pos.size_usd,
            "fee": pos.fee,
            "pnl": pnl,
            "opened_at": pos.opened_at.isoformat(),
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "slug": pos.slug,
        }
        self.closed.append(record)
        logger.info(f"Resolved {order_id} | pnl=${pnl:.3f}")
        return record

    def get_open_list(self) -> List[OpenPosition]:
        return list(self.open.values())

    def summary(self) -> Dict:
        total_pnl = sum(t["pnl"] for t in self.closed)
        wins = sum(1 for t in self.closed if t["pnl"] > 0)
        n = len(self.closed)
        return {
            "open_count": len(self.open),
            "closed_count": n,
            "total_pnl": total_pnl,
            "win_rate": wins / n if n else 0.0,
        }
