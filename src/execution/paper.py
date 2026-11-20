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

from src.utils.fees import taker_fee_usd

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
        fee_exponent: float = 1.0,
        extra_slippage: float = 0.0,
        prefer_maker: bool = True,
        slippage_bps: float = 8.0,
        max_slippage_bps: float = 100.0,
    ):
        self.fee_rate = max(0.0, min(0.25, _finite(fee_rate, 0.07)))
        self.fee_exponent = max(0.1, min(4.0, _finite(fee_exponent, 1.0)))
        self.extra_slippage = max(0.0, _finite(extra_slippage, 0.0))
        # Retained for config compatibility. A maker rebate is never assumed:
        # the caller does not provide evidence of a resting maker fill.
        self.prefer_maker = bool(prefer_maker)
        self.slippage_bps = max(0.0, min(500.0, _finite(slippage_bps, 8.0)))
        self.max_slippage_bps = max(0.0, min(5000.0, _finite(max_slippage_bps, 100.0)))
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
        fee_exponent: Optional[float] = None,
        book_asks: Optional[list] = None,
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

        effective_fee_exponent = self.fee_exponent if fee_exponent is None else max(0.1, min(4.0, _finite(fee_exponent, self.fee_exponent)))

        # All current strategy calls are aggressive. If the caller supplies
        # the live ask ladder, consume actual displayed depth and reject a
        # requested notional that cannot be filled inside the slippage budget.
        slip = max(self.extra_slippage, requested * self.slippage_bps / 10_000.0)
        ladder_fee = None
        ladder_fee_shares = 0.0
        if side == "buy" and book_asks is not None:
            remaining = notional
            shares = 0.0
            spent = 0.0
            ladder_fee = 0.0
            valid_levels = []
            for level in book_asks:
                try:
                    level_price = float(level.get("price")) if isinstance(level, dict) else float(level[0])
                    level_size = float(level.get("size")) if isinstance(level, dict) else float(level[1])
                except (TypeError, ValueError, IndexError, AttributeError):
                    continue
                if 0 < level_price < 1 and math.isfinite(level_size) and level_size > 0:
                    valid_levels.append((level_price, level_size))
            valid_levels.sort(key=lambda item: item[0])
            if not valid_levels or abs(valid_levels[0][0] - requested) > 1e-6:
                logger.warning("PAPER SKIP | supplied ask ladder does not match requested best ask")
                return None
            for level_price, level_size in valid_levels:
                adjusted = level_price + max(self.extra_slippage, level_price * self.slippage_bps / 10_000.0)
                if not 0 < adjusted < 1:
                    continue
                capacity = level_size * adjusted
                spend = min(remaining, capacity)
                if spend <= 0:
                    continue
                level_shares = spend / adjusted
                shares += level_shares
                # Polymarket's fee curve is price-dependent. Applying the fee
                # once at VWAP is not equivalent when a fill walks multiple
                # price levels, so calculate each level's fee independently.
                level_fee = taker_fee_usd(
                    level_shares, adjusted,
                    rate=self.fee_rate if fee_rate is None else max(0.0, min(0.25, _finite(fee_rate, self.fee_rate))),
                    exponent=effective_fee_exponent,
                )
                ladder_fee += level_fee
                ladder_fee_shares += level_fee / adjusted
                spent += spend
                remaining -= spend
                if remaining <= max(1e-8, notional * 1e-8):
                    remaining = 0.0
                    break
            if remaining > max(1e-6, notional * 1e-6) or shares <= 0:
                logger.warning(
                    "PAPER SKIP | insufficient executable ask depth | requested=$%.2f unfilled=$%.2f",
                    notional, remaining,
                )
                return None
            fill_price = spent / shares
            slip = fill_price - requested
            realized_slippage_bps = slip / requested * 10_000.0
            if realized_slippage_bps > self.max_slippage_bps:
                logger.warning(
                    "PAPER SKIP | book-walk slippage %.1f bps exceeds %.1f bps cap",
                    realized_slippage_bps, self.max_slippage_bps,
                )
                return None
            notional = spent
        else:
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
        fee = ladder_fee if ladder_fee is not None else taker_fee_usd(
            shares, fill_price, rate=effective_fee_rate, exponent=effective_fee_exponent
        )
        # On Polymarket.com, BUY taker fees are deducted in shares; SELL
        # taker fees are deducted in USDC. Keep gross and net share quantities
        # separate so the ledger can audit the fee and settle only received shares.
        fee_shares = (
            ladder_fee_shares if ladder_fee is not None
            else (fee / fill_price if side == "buy" and fill_price > 0 else 0.0)
        )
        net_shares = shares - fee_shares if side == "buy" else shares
        if net_shares <= 0 or not math.isfinite(net_shares):
            logger.warning("PAPER SKIP | buy fee consumes all received shares")
            return None
        effective_entry_price = notional / net_shares if side == "buy" else fill_price
        residual_fee_usd = 0.0 if side == "buy" else fee
        fee_frac = fee / notional if notional > 0 else 0.0
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
            "fee_shares": fee_shares,
            "net_shares": net_shares,
            "effective_entry_price": effective_entry_price,
            "residual_fee_usd": residual_fee_usd,
            "fee": fee,
            "fee_frac": fee_frac,
            "fee_rate": effective_fee_rate,
            "fee_exponent": effective_fee_exponent,
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
        pnl = (
            (resolution - order.get("effective_entry_price", order["fill_price"]))
            * order.get("net_shares", order["shares"])
            - order.get("residual_fee_usd", order["fee"])
        )
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
