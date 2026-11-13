"""
Feature engineering for short-term crypto Up/Down markets.
"""
from typing import Dict, Optional, Any
import logging
from datetime import datetime, timezone

logger = logging.getLogger("astf.features")


def _level_price(level: Any) -> float:
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
    if "5m" in text or "5 min" in text or "5-minute" in text or "5-min" in text:
        return 5 * 60
    if "15m" in text or "15 min" in text or "15-minute" in text or "15-min" in text:
        return 15 * 60
    return 15 * 60


def compute_time_features(end_date_iso: Optional[str], window_seconds: int = 900) -> Dict[str, float]:
    if not end_date_iso:
        return {"time_remaining_s": float(window_seconds) * 0.5, "time_remaining_pct": 0.5}
    try:
        end = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        remaining = (end - now).total_seconds()
        if remaining <= 0:
            return {"time_remaining_s": 0.0, "time_remaining_pct": 0.0}
        pct = min(1.0, remaining / max(window_seconds, 1))
        return {"time_remaining_s": float(remaining), "time_remaining_pct": float(pct)}
    except Exception:
        return {"time_remaining_s": float(window_seconds) * 0.5, "time_remaining_pct": 0.5}


def compute_book_imbalance(bids: list, asks: list, levels: int = 5) -> float:
    bid_vol = sum(_level_size(b) for b in bids[:levels])
    ask_vol = sum(_level_size(a) for a in asks[:levels])
    total = bid_vol + ask_vol
    if total <= 0:
        return 0.0
    return (bid_vol - ask_vol) / total


def compute_depth_usd(bids: list, asks: list, levels: int = 5) -> float:
    depth = 0.0
    for b in bids[:levels]:
        depth += _level_price(b) * _level_size(b)
    for a in asks[:levels]:
        depth += _level_price(a) * _level_size(a)
    return depth


def best_ask(book: Dict) -> Optional[float]:
    asks = book.get("asks") or []
    if not asks:
        return book.get("mid")
    return _level_price(asks[0]) or book.get("mid")


def best_bid(book: Dict) -> Optional[float]:
    bids = book.get("bids") or []
    if not bids:
        return book.get("mid")
    return _level_price(bids[0]) or book.get("mid")


def cross_book_imbalance(book_up: Dict, book_down: Dict) -> float:
    """
    Positive → market leaning UP (more bid pressure on up / ask pressure on down).
    Uses relative mid skew when both mids exist, blended with single-side imbalances.
    """
    up_mid = book_up.get("mid")
    dn_mid = book_down.get("mid")
    skew = 0.0
    if up_mid is not None and dn_mid is not None:
        try:
            u, d = float(up_mid), float(dn_mid)
            # If up is expensive relative to down, market prices UP higher
            skew = max(-1.0, min(1.0, (u - d) * 2.0))
        except (TypeError, ValueError):
            skew = 0.0

    imb_up = compute_book_imbalance(book_up.get("bids") or [], book_up.get("asks") or [])
    imb_dn = compute_book_imbalance(book_down.get("bids") or [], book_down.get("asks") or [])
    # Bid-heavy up book and ask-heavy down book both support UP
    side = 0.5 * imb_up - 0.5 * imb_dn
    return max(-1.0, min(1.0, 0.55 * skew + 0.45 * side))


def build_features(
    market: Dict,
    book: Dict,
    external_momentum: float = 0.0,
    side: str = "up",
    book_other: Optional[Dict] = None,
) -> Dict:
    bids = book.get("bids", []) or []
    asks = book.get("asks", []) or []

    mid = book.get("mid")
    if mid is None and bids and asks:
        mid = (_level_price(bids[0]) + _level_price(asks[0])) / 2

    ask = best_ask(book)
    bid = best_bid(book)

    slug = market.get("slug") or market.get("market_slug") or ""
    question = market.get("question") or ""
    window_s = int(market.get("_window_seconds") or detect_window_seconds(slug, question))
    end_iso = market.get("endDate") or market.get("end_date_iso")
    tfeat = compute_time_features(end_iso, window_s)

    # Prefer true cross-book signal when both books available
    if book_other is not None:
        if side == "up":
            imbalance = cross_book_imbalance(book, book_other)
        else:
            imbalance = -cross_book_imbalance(book_other, book)
    else:
        imbalance = compute_book_imbalance(bids, asks)
        # Single-book imbalance is already side-local; no extra flip here

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
        "time_remaining_pct": tfeat["time_remaining_pct"],
        "time_remaining_s": tfeat["time_remaining_s"],
        "mid_price": mid,
        "ask_price": ask,
        "bid_price": bid,
        "spread": spread,
        "depth_usd": depth,
        "window_seconds": window_s,
        "market_slug": slug,
        "side": side,
    }
