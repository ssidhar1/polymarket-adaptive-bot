"""
Polymarket crypto_fees_v2 approximation.

Observed feeSchedule on short BTC/ETH windows:
  {"exponent": 1, "rate": 0.07, "takerOnly": true, "rebateRate": 0.2}

Taker fee fraction of notional ≈ rate * p * (1 - p)
(peaks near mid; cheaper at extremes — still bad EV if you buy 0.90).
"""
from typing import Optional


def taker_fee_rate(price: float, rate: float = 0.07, exponent: int = 1) -> float:
    p = float(price)
    if p <= 0.0 or p >= 1.0:
        return 0.0
    # rate * p^exp * (1-p)^exp — exp=1 → rate * p * (1-p)
    return float(rate) * (p ** exponent) * ((1.0 - p) ** exponent)


def fee_on_notional(notional_usd: float, price: float, rate: float = 0.07) -> float:
    return max(0.0, float(notional_usd) * taker_fee_rate(price, rate=rate))


def edge_after_taker_fee(est_prob: float, entry: float, rate: float = 0.07) -> float:
    """Expected edge per $1 notional after taker fee (resolution to 0/1)."""
    fee = taker_fee_rate(entry, rate=rate)
    return float(est_prob) - float(entry) - fee


def parse_fee_schedule(market: Optional[dict], default_rate: float = 0.07) -> float:
    if not market:
        return default_rate
    fs = market.get("feeSchedule") or {}
    try:
        return float(fs.get("rate", default_rate))
    except (TypeError, ValueError):
        return default_rate
