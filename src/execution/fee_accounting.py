"""Conservative fee-coverage accounting for exchange fills.

Only explicit USDC fee amounts from exchange records count as paid fees.
A fee rate in basis points is metadata, not a fee amount; this module never
converts it into dollars or reports net P&L from it.
"""
from decimal import Decimal, InvalidOperation


def explicit_fee_usdc(trade):
    """Return a validated explicit fee amount, or None when not supplied."""
    if isinstance(trade, dict):
        raw = trade.get("fee_usdc", trade.get("fee_amount_usdc"))
    else:
        raw = getattr(trade, "fee_usdc", getattr(trade, "fee_amount_usdc", None))
    if raw is None:
        return None
    try:
        fee = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("invalid explicit fee amount") from None
    if not fee.is_finite() or fee < 0:
        raise ValueError("invalid explicit fee amount")
    return fee


def fee_coverage(fills):
    """Summarize whether all fills have explicit fees; never infer missing fees."""
    rows = list(fills)
    known = [Decimal(str(row["fee_amount_usdc"])) for row in rows
             if row.get("fee_amount_usdc") is not None]
    missing = len(rows) - len(known)
    return {
        "fill_count": len(rows),
        "explicit_fee_count": len(known),
        "missing_fee_count": missing,
        "explicit_fees_usdc": sum(known, Decimal("0")) if not missing else None,
        "fee_complete": bool(rows) and missing == 0,
        "net_realized_pnl_available": False,
        "reason": ("settlement and complete fee records are still required"
                   if not rows or missing == 0 else "one or more fills lack explicit fee amounts"),
    }
