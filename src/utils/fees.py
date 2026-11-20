"""Polymarket taker-fee calculations.

Polymarket's published formula is fee_usd = shares * fee_rate * p * (1-p).
The returned taker_fee_rate is therefore a USDC-equivalent fee per share,
not a percentage of dollar notional. Buy-side fees may be collected in shares;
the ledger stores their USDC value at execution for consistent P&L accounting.
"""
from typing import Optional
import math


def taker_fee_rate(price: float, rate: float = 0.07, exponent: int = 1) -> float:
    """Return the USDC-equivalent fee per share at the execution price."""
    p = float(price)
    coefficient = float(rate)
    if not math.isfinite(p) or not math.isfinite(coefficient) or p <= 0.0 or p >= 1.0:
        return 0.0
    exp = max(1, int(exponent))
    return max(0.0, coefficient) * (p ** exp) * ((1.0 - p) ** exp)


def taker_fee_usd(shares: float, price: float, rate: float = 0.07,
                  exponent: int = 1) -> float:
    """Calculate fee in USDC, rounded to 5 decimals; sub-increment fees round to zero."""
    quantity = float(shares)
    if not math.isfinite(quantity) or quantity <= 0:
        return 0.0
    raw = quantity * taker_fee_rate(price, rate=rate, exponent=exponent)
    if raw <= 0.0:
        return 0.0
    # The published precision is five decimals. Do not impose a minimum
    # charge: a raw fee below half a 0.00001 increment rounds to zero.
    return round(raw, 5)


def fee_on_notional(notional_usd: float, price: float, rate: float = 0.07) -> float:
    """Calculate fee from dollar notional by converting notional to shares."""
    p = float(price)
    notional = float(notional_usd)
    if not math.isfinite(p) or not math.isfinite(notional) or p <= 0.0 or p >= 1.0 or notional <= 0:
        return 0.0
    return taker_fee_usd(notional / p, p, rate=rate)


def edge_after_taker_fee(est_prob: float, entry: float, rate: float = 0.07) -> float:
    """Expected edge per share after the price-dependent taker fee."""
    fee_per_share = taker_fee_rate(entry, rate=rate)
    return float(est_prob) - float(entry) - fee_per_share


def parse_fee_schedule(market: Optional[dict], default_rate: float = 0.07) -> float:
    if not isinstance(market, dict):
        return default_rate
    if market.get("_fee_rate") is not None:
        try:
            rate = float(market["_fee_rate"])
            if math.isfinite(rate) and 0 <= rate <= 0.25:
                return rate
        except (TypeError, ValueError):
            pass
    if market.get("feesEnabled") is False:
        return 0.0
    fs = market.get("feeSchedule") or {}
    if isinstance(fs, dict):
        try:
            rate = float(fs.get("rate", default_rate))
            return rate if math.isfinite(rate) and 0 <= rate <= 0.25 else default_rate
        except (TypeError, ValueError):
            pass
    return default_rate


def parse_fee_exponent(market: Optional[dict], default_exponent: float = 1.0) -> float:
    if not isinstance(market, dict):
        return default_exponent
    raw = market.get("_fee_exponent")
    if raw is None:
        fs = market.get("feeSchedule") or {}
        raw = fs.get("exponent", default_exponent) if isinstance(fs, dict) else default_exponent
    try:
        exponent = float(raw)
        return exponent if math.isfinite(exponent) and 0 < exponent <= 4 else default_exponent
    except (TypeError, ValueError):
        return default_exponent
