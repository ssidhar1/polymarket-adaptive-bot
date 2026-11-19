"""
Conservative paper execution for Polymarket binary tokens.

The strategy currently submits aggressive orders. Those must be modeled as
taker fills: no maker rebate, adverse slippage, and explicit rejection when
the simulated fill would leave the binary-token price domain.
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
        value = float(x)
        if not math.isfinite(value):
            return default
        return value
    except (TypeError, ValueError):
        return default


class PaperExecutor:
    def __init__(
        self,
        fee_rate: float = 0.07,
        extra_slippage: float = 0.0,
        prefer_maker: bool = True,
        slippage_bps: float = 8.0,
    ):
        self.fee_rate = max(0.0, min(0.25, _finite(fee_rate, 0.07)))
        self.extra_slippage = max(0.0, _finite(extra_slippage, 0.0))
        # Retained for config compatibility. A maker rebate is never assumed:
        # the caller does not provide evidence of a resting maker fill.
        self.prefer_maker = bool(prefer_maker)
        self.slippage_bps = max(0.0, min(500.0, _finite(slippage_bps, 8.0)))
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
        fee_rate: Optional[float] = None,
    ) -> Optional[Dict]:
        if not token_id or not market_id:
            logger.warning("PAPER SKIP | missing token_id or market_id")
            return None

        side = (side or "buy").lower()
        if side not in ("buy", "sell"):
            logger.warning(f"PAPER SKIP | bad side={side}")
            return None

        requested = _finite(price, 0.0)
        notional = _finite(size_usd, 0.0)
        if not (0.0 < requested < 1.0) or notional < MIN_NOTIONAL:
            logger.warning(f"PAPER SKIP | price={requested} size={notional}")
            return None

        # All current strategy calls are aggressive. Apply adverse slippage
        # to both aggressive and unverified non-aggressive fills; never grant
        # a passive fill or rebate without actual queue/fill evidence.
        slip = max(self.extra_slippage, requested * self.slippage_bps / 10_000.0)
        fill_price = requested + slip if side == "buy" else requested - slip
        if not (0.0 < fill_price < 1.0):
            logger.warning(
                "PAPER SKIP | slippage pushes fill outside binary price range "
                f"(requested={requested:.4f}, slip={slip:.5f})"
            )
            return None

        shares = notional / fill_price
        if shares <= 0 or not math.isfinite(shares):
            logger.warning("PAPER SKIP | non-finite shares")
            return None

        # No maker rebate: aggressive orders cross the spread and pay taker fees.
        effective_fee_rate = self.fee_rate if fee_rate is None else max(0.0, min(0.25, _finite(fee_rate, self.fee_rate)))
        fee_frac = taker_fee_rate(fill_price, rate=effective_fee_rate)
        fee = notional * fee_frac
        order_id = str(uuid.uuid4())[:8]

        order = {
            "order_id": order_id,
            "token_id": str(token_id),
            "market_id": str(market_id),
            "side": side,
            "requested_price": requested,
            "fill_price": fill_price,
            "slippage": slip,
            "slippage_bps": (slip / requested) * 10_000.0,
            "size_usd": notional,
            "shares": shares,
            "fee": fee,
            "fee_frac": fee_frac,
            "fee_rate": effective_fee_rate,
            "maker": False,
            "execution_type": "taker",
            "status": "filled",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.open_orders[order_id] = order
        logger.info(
            f"PAPER FILL | {side.upper()} {shares:.2f} @ {fill_price:.4f} "
            f"(requested={requested:.4f}, slip={slip:.5f}) | "
            f"taker_fee=${fee:.4f} ({fee_frac:.3%})"
        )
        return order

    def resolve(self, order_id: str, resolution_price: float) -> Optional[Dict]:
        if order_id not in self.open_orders:
            return None
        order = self.open_orders.pop(order_id)
        resolution = _finite(resolution_price, 0.5)
        resolution = max(0.0, min(1.0, resolution))
        pnl = (resolution - order["fill_price"]) * order["shares"] - order["fee"]
        if not math.isfinite(pnl):
            pnl = -order["fee"]
        trade = {
            **order,
            "resolution": resolution,
            "pnl": pnl,
            "closed_at": datetime.now(timezone.utc).isoformat(),
        }
        self.closed_trades.append(trade)
        if len(self.closed_trades) > 500:
            self.closed_trades = self.closed_trades[-500:]
        logger.info(f"PAPER RESOLVE | pnl=${pnl:.4f}")
        return trade
