"""
Paper trading engine - simulates fills with realistic fees and slippage.
"""
from typing import Dict, Optional
from datetime import datetime, timezone
import logging
import math
import uuid

from src.utils.fees import taker_fee_rate

logger = logging.getLogger("astf.execution.paper")

MIN_NOTIONAL = 0.50


def _finite(x: float, default: float = 0.0) -> float:
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except (TypeError, ValueError):
        return default


class PaperExecutor:
    def __init__(self, fee_rate: float = 0.07, extra_slippage: float = 0.0):
        # fee_rate here is the schedule "rate" (e.g. 0.07), not flat %
        self.fee_rate = max(0.0, min(0.25, float(fee_rate)))
        self.extra_slippage = max(0.0, float(extra_slippage or 0.0))
        self.open_orders = {}
        self.closed_trades = []

    def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size_usd: float,
        market_id: str,
        already_aggressive: bool = True,
    ) -> Optional[Dict]:
        if not token_id or not market_id:
            logger.warning("PAPER SKIP | missing token_id or market_id")
            return None

        side = (side or "buy").lower()
        if side not in ("buy", "sell"):
            logger.warning(f"PAPER SKIP | bad side={side}")
            return None

        px = _finite(price, 0.0)
        cost = _finite(size_usd, 0.0)
        if px <= 0 or cost < MIN_NOTIONAL:
            logger.warning(f"PAPER SKIP | price={px} size={cost}")
            return None

        if not already_aggressive:
            slip = self.extra_slippage if self.extra_slippage else 0.002
            px = px + slip if side == "buy" else px - slip
        fill_price = max(0.01, min(0.99, px))

        shares = cost / fill_price if fill_price > 0 else 0.0
        if shares <= 0 or not math.isfinite(shares):
            logger.warning("PAPER SKIP | non-finite shares")
            return None

        fee_frac = taker_fee_rate(fill_price, rate=self.fee_rate)
        fee = cost * fee_frac
        order_id = str(uuid.uuid4())[:8]

        order = {
            "order_id": order_id,
            "token_id": str(token_id),
            "market_id": str(market_id),
            "side": side,
            "requested_price": float(price),
            "fill_price": fill_price,
            "size_usd": cost,
            "shares": shares,
            "fee": fee,
            "fee_frac": fee_frac,
            "status": "filled",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.open_orders[order_id] = order
        logger.info(
            f"PAPER FILL | {side.upper()} {shares:.2f} @ {fill_price:.3f} | "
            f"fee=${fee:.3f} ({fee_frac:.2%})"
        )
        return order

    def resolve(self, order_id: str, resolution_price: float) -> Optional[Dict]:
        if order_id not in self.open_orders:
            return None
        order = self.open_orders.pop(order_id)
        res = _finite(resolution_price, 0.5)
        res = max(0.0, min(1.0, res))
        pnl = (res - order["fill_price"]) * order["shares"] - order["fee"]
        if not math.isfinite(pnl):
            pnl = -order["fee"]
        trade = {
            **order,
            "resolution": res,
            "pnl": pnl,
            "closed_at": datetime.now(timezone.utc).isoformat(),
        }
        self.closed_trades.append(trade)
        if len(self.closed_trades) > 500:
            self.closed_trades = self.closed_trades[-500:]
        logger.info(f"PAPER RESOLVE | pnl=${pnl:.3f}")
        return trade
