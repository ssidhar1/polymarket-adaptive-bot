"""
Feature engineering for short-term crypto Up/Down markets.
"""
from typing import Dict, Optional
import logging
from datetime import datetime, timezone

logger = logging.getLogger("astf.features")

def compute_time_remaining_pct(end_date_iso: Optional[str]) -> float:
    if not end_date_iso:
        return 0.5
    try:
        end = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        total = 15 * 60  # assume 15m window as default
        remaining = max(0.0, (end - now).total_seconds())
        return min(1.0, remaining / total)
    except Exception:
        return 0.5

def compute_book_imbalance(bids: list, asks: list, levels: int = 5) -> float:
    """
    Simple imbalance: (bid_volume - ask_volume) / (bid + ask) in top levels.
    Positive = more buying pressure.
    """
    bid_vol = sum(float(b.get("size", 0) or 0) for b in bids[:levels])
    ask_vol = sum(float(a.get("size", 0) or 0) for a in asks[:levels])
    total = bid_vol + ask_vol
    if total <= 0:
        return 0.0
    return (bid_vol - ask_vol) / total

def build_features(market: Dict, book: Dict, external_momentum: float = 0.0) -> Dict:
    """
    Build the feature dict expected by SignalScorer.
    """
    bids = book.get("bids", []) or []
    asks = book.get("asks", []) or []

    mid = None
    if bids and asks:
        best_bid = float(bids[0].get("price", 0))
        best_ask = float(asks[0].get("price", 1))
        mid = (best_bid + best_ask) / 2

    imbalance = compute_book_imbalance(bids, asks)
    time_pct = compute_time_remaining_pct(market.get("endDate") or market.get("end_date_iso"))

    # Very rough vol regime from spread
    spread = 0.0
    if bids and asks:
        spread = float(asks[0].get("price", 1)) - float(bids[0].get("price", 0))
    vol_regime = "high" if spread > 0.04 else ("low" if spread < 0.015 else "normal")

    return {
        "momentum": external_momentum,          # to be filled from price feed later
        "book_imbalance": imbalance,
        "volatility_regime": vol_regime,
        "time_remaining_pct": time_pct,
        "mid_price": mid,
        "spread": spread,
        "market_slug": market.get("slug") or market.get("market_slug"),
    }
