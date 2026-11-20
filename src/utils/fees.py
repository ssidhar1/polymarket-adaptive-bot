"""Polymarket taker-fee calculations.

Polymarket's published formula is fee_usd = shares * fee_rate * p * (1-p).
The returned taker_fee_rate is therefore a USDC-equivalent fee per share,
not a percentage of dollar notional. Buy-side fees may be collected in shares;
the ledger stores their USDC value at execution for consistent P&L accounting.
"""
from typing import Optional


def taker_fee_rate(price: float, rate: float = 0.07, exponent: int = 1) -> float:
    """Return the USDC-equivalent fee per share at the execution price."""
    p = float(price)
    if p <= 0.0 or p >= 1.0:
        return 0.0
    exp = max(1, int(exponent))
    return max(0.0, float(rate)) * (p ** exp) * ((1.0 - p) ** exp)


def taker_fee_usd(shares: float, price: float, rate: float = 0.07,
                  exponent: int = 1, min_fee: float = 0.00001) -> float:
    """Calculate fee in USDC, rounded to 5 decimals with the published minimum."""
    quantity = max(0.0, float(shares))
    raw = quantity * taker_fee_rate(price, rate=rate, exponent=exponent)
    if raw <= 0.0:
        return 0.0
    rounded = round(raw, 5)
    return max(float(min_fee), rounded)


def fee_on_notional(notional_usd: float, price: float, rate: float = 0.07) -> float:
    """Calculate fee from dollar notional by converting notional to shares."""
    p = float(price)
    if p <= 0.0 or p >= 1.0:
        return 0.0
    return taker_fee_usd(float(notional_usd) / p, p, rate=rate)


def edge_after_taker_fee(est_prob: float, entry: float, rate: float = 0.07) -> float:
    """Expected edge per share after the price-dependent taker fee."""
    fee_per_share = taker_fee_rate(entry, rate=rate)
    return float(est_prob) - float(entry) - fee_per_share


def parse_fee_schedule(market: Optional[dict], default_rate: float = 0.07) -> float:
    if not market:
        return default_rate
    fs = market.get("feeSchedule") or {}
    try:
        return float(fs.get("rate", default_rate))
    except (TypeError, ValueError):
        return default_rate
