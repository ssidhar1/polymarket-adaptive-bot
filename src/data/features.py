"""
Feature engineering for short-term crypto Up/Down markets.
"""
from typing import Dict, Optional, Any
import logging
from datetime import datetime, timezone

logger = logging.getLogger("astf.features")


def _level_price(level: Any) -> float:
    """Support both {price, size} dicts and [price, size] lists."""
    try:
        if isinstance(level, dict):
            return float(level.get("price", 0) or 0)
        if isinstance(level, (list, tuple)) and len(level) >= 1:
            return float(level[0])
    except (TypeError, ValueError):
        pass
    return 0.0


def _level_size(level: Any) -> float:
    try:
        if isinstance(level, dict):
            return float(level.get("size", 0) or 0)
        if isinstance(level, (list, tuple)) and len(level) >= 2:
            return float(level[1])
    except (TypeError, ValueError):
        pass
    return 0.0


def detect_window_seconds(slug: Optional[str], question: Optional[str] = None) -> int:
    text = f"{slug or ''} {question or ''}".lower()
    if "5m" in text or "5 min" in text or "5-minute" in text:
        return 5 * 60
    return 15 * 60


def compute_time_remaining_pct(end_date_iso: Optional[str], window_seconds: int = 900) -> float:
    if not end_date_iso:
        return 0.5
    try:
        end = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        remaining = max(0.0, (end - now).total_seconds())
        return min(1.0, remaining / max(window_seconds, 1))
    except Exception:
        return 0.5


def compute_book_imbalance(bids: list, asks: list, levels: int = 5) -> float:
    bid_vol = sum(_level_size(b) for b in bids[:levels])
    ask_vol = sum(_level_size(a) for a in asks[:levels])
    total = bid_vol + ask_vol
    if total <= 0:
        return 0.0
    return (bid_vol - ask_vol) / total


def compute_depth_usd(bids: list, asks: list, levels: int = 5) -> float:
    """Approximate top-of-book notional depth."""
    depth = 0.0
    for b in bids[:levels]:
        depth += _level_price(b) * _level_size(b)
    for a in asks[:levels]:
        depth += _level_price(a) * _level_size(a)
    return depth


def build_features(market: Dict, book: Dict, external_momentum: float = 0.0) -> Dict:
    bids = book.get("bids", []) or []
    asks = book.get("asks", []) or []

    mid = book.get("mid")
    if mid is None and bids and asks:
        mid = (_level_price(bids[0]) + _level_price(asks[0])) / 2

    slug = market.get("slug") or market.get("market_slug") or ""
    question = market.get("question") or ""
    window_s = detect_window_seconds(slug, question)
    end_iso = market.get("endDate") or market.get("end_date_iso")
    time_pct = compute_time_remaining_pct(end_iso, window_s)

    imbalance = compute_book_imbalance(bids, asks)
    depth = compute_depth_usd(bids, asks)

    spread = 0.0
    if bids and asks:
        spread = max(0.0, _level_price(asks[0]) - _level_price(bids[0]))

    if spread > 0.04:
        vol_regime = "high"
    elif spread < 0.015:
        vol_regime = "low"
    else:
        vol_regime = "normal"

    return {
        "momentum": external_momentum,
        "book_imbalance": imbalance,
        "volatility_regime": vol_regime,
        "time_remaining_pct": time_pct,
        "mid_price": mid,
        "spread": spread,
        "depth_usd": depth,
        "window_seconds": window_s,
        "market_slug": slug,
    }
