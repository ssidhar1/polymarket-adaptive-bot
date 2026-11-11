"""
Paper trading engine - simulates fills with realistic fees and slippage.
"""
from typing import Dict, Optional
from datetime import datetime
import logging
import uuid

logger = logging.getLogger("astf.execution.paper")

class PaperExecutor:
    def __init__(self, fee_rate: float = 0.02):
        self.fee_rate = fee_rate  # approximate taker fee
        self.open_orders = {}
        self.closed_trades = []

    def place_order(self, token_id: str, side: str, price: float, size_usd: float, market_id: str) -> Dict:
        order_id = str(uuid.uuid4())[:8]
        # Simulate immediate fill at requested price +/- small slippage
        slippage = 0.002 if side == "buy" else -0.002
        fill_price = max(0.01, min(0.99, price + slippage))

        cost = size_usd
        shares = size_usd / fill_price
        fee = cost * self.fee_rate

        order = {
            "order_id": order_id,
            "token_id": token_id,
            "market_id": market_id,
            "side": side,
            "requested_price": price,
            "fill_price": fill_price,
            "size_usd": size_usd,
            "shares": shares,
            "fee": fee,
            "status": "filled",
            "timestamp": datetime.utcnow().isoformat()
        }
        self.open_orders[order_id] = order
        logger.info(f"PAPER FILL | {side.upper()} {shares:.2f} @ {fill_price:.3f} | fee=${fee:.3f}")
        return order

    def resolve(self, order_id: str, resolution_price: float) -> Optional[Dict]:
        if order_id not in self.open_orders:
            return None
        order = self.open_orders.pop(order_id)
        # For Up/Down: resolution_price is 1.0 or 0.0
        pnl = (resolution_price - order["fill_price"]) * order["shares"] - order["fee"]
        trade = {**order, "resolution": resolution_price, "pnl": pnl, "closed_at": datetime.utcnow().isoformat()}
        self.closed_trades.append(trade)
        logger.info(f"PAPER RESOLVE | pnl=${pnl:.3f}")
        return trade
