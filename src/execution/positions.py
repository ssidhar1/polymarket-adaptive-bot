"""
Position tracker + resolution logic for paper (and later live) trades.
Captures entry timing fields for later hour/weekday edge analysis.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone, timedelta
import logging

logger = logging.getLogger("astf.positions")


@dataclass
class OpenPosition:
    order_id: str
    market_id: str
    token_id: str
    direction: str
    side: str
    entry_price: float
    size_usd: float
    shares: float
    fee: float
    opened_at: datetime
    slug: str = ""
    end_date: Optional[str] = None
    # Timing / signal meta for tuning
    asset: str = ""
    window: str = ""  # 5m / 15m
    hour_utc: int = -1
    weekday_utc: int = -1  # Mon=0 .. Sun=6
    time_remaining_s: float = 0.0
    time_remaining_pct: float = 0.0
    score: float = 0.0
    edge_after_fees: float = 0.0
    momentum: float = 0.0
    regime: str = ""
    trade_type: str = "dir"  # dir | arb | expl


class PositionTracker:
    def __init__(self):
        self.open: Dict[str, OpenPosition] = {}
        self.closed: List[Dict] = []

    def add(
        self,
        order: Dict,
        direction: str,
        slug: str = "",
        end_date: str = None,
        meta: Optional[Dict[str, Any]] = None,
    ):
        meta = meta or {}
        opened = datetime.now(timezone.utc)
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
            opened_at=opened,
            slug=slug or "",
            end_date=end_date,
            asset=str(meta.get("asset") or ""),
            window=str(meta.get("window") or ""),
            hour_utc=int(meta.get("hour_utc", opened.hour)),
            weekday_utc=int(meta.get("weekday_utc", opened.weekday())),
            time_remaining_s=float(meta.get("time_remaining_s") or 0.0),
            time_remaining_pct=float(meta.get("time_remaining_pct") or 0.0),
            score=float(meta.get("score") or 0.0),
            edge_after_fees=float(meta.get("edge_after_fees") or 0.0),
            momentum=float(meta.get("momentum") or 0.0),
            regime=str(meta.get("regime") or ""),
            trade_type=str(meta.get("trade_type") or "dir"),
        )
        self.open[order["order_id"]] = pos
        logger.info(
            f"Opened {order['order_id']} {direction} @ {order['fill_price']:.3f} | "
            f"h={pos.hour_utc} wd={pos.weekday_utc} win={pos.window} "
            f"t_left={pos.time_remaining_s:.0f}s"
        )

    def resolve(self, order_id: str, resolution_price: float) -> Optional[Dict]:
        if order_id not in self.open:
            return None
        pos = self.open.pop(order_id)
        closed_at = datetime.now(timezone.utc)
        pnl = (resolution_price - pos.entry_price) * pos.shares - pos.fee
        hold_s = (closed_at - pos.opened_at).total_seconds()
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
            "closed_at": closed_at.isoformat(),
            "slug": pos.slug,
            "asset": pos.asset,
            "window": pos.window,
            "hour_utc": pos.hour_utc,
            "weekday_utc": pos.weekday_utc,
            "time_remaining_s": pos.time_remaining_s,
            "time_remaining_pct": pos.time_remaining_pct,
            "score": pos.score,
            "edge_after_fees": pos.edge_after_fees,
            "momentum": pos.momentum,
            "regime": pos.regime,
            "trade_type": pos.trade_type,
            "hold_seconds": hold_s,
            "win": 1 if pnl > 0 else 0,
        }
        self.closed.append(record)
        logger.info(
            f"Resolved {order_id} | pnl=${pnl:.3f} | "
            f"h={pos.hour_utc} wd={pos.weekday_utc} win={pos.window} hold={hold_s:.0f}s"
        )
        return record

    def get_open_list(self) -> List[OpenPosition]:
        return list(self.open.values())

    def open_count(self) -> int:
        return len(self.open)

    def is_stale(self, pos: OpenPosition, grace_seconds: int = 180) -> bool:
        now = datetime.now(timezone.utc)
        if pos.end_date:
            try:
                end = datetime.fromisoformat(pos.end_date.replace("Z", "+00:00"))
                if now > end + timedelta(seconds=grace_seconds):
                    return True
            except Exception:
                pass
        if now - pos.opened_at > timedelta(minutes=25):
            return True
        return False

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
