"""
Feature engineering for short-term crypto Up/Down markets.
"""
from typing import Dict, Optional, Any
import logging
import math
from datetime import datetime, timezone

from src.utils.fees import parse_fee_schedule, parse_fee_exponent

logger = logging.getLogger("astf.features")


def _level_price(level: Any) -> float:
    try:
        if isinstance(level, dict):
            value = float(level.get("price", 0) or 0)
        elif isinstance(level, (list, tuple)) and len(level) >= 1:
            value = float(level[0])
        else:
            return 0.0
        return value if math.isfinite(value) and 0.0 < value < 1.0 else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _level_size(level: Any) -> float:
    try:
        if isinstance(level, dict):
            value = float(level.get("size", 0) or 0)
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            value = float(level[1])
        else:
            return 0.0
        return value if math.isfinite(value) and value > 0.0 else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _valid_levels(levels: list) -> list:
    valid = []
    for level in levels or []:
        price, size = _level_price(level), _level_size(level)
        if price > 0.0 and size > 0.0:
            valid.append({"price": price, "size": size})
    return valid


def detect_window_seconds(slug: Optional[str], question: Optional[str] = None) -> int:
    text = f"{slug or ''} {question or ''}".lower()
    # Check 15m first: the substring "5m" is contained in "15m".
    if "15m" in text or "15 min" in text or "15-minute" in text or "15-min" in text:
        return 15 * 60
    if "5m" in text or "5 min" in text or "5-minute" in text or "5-min" in text:
        return 5 * 60
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
    """Return an actually quoted ask; a midpoint is not executable liquidity."""
    asks = book.get("asks") or []
    for level in asks:
        price = _level_price(level)
        if price > 0.0 and _level_size(level) > 0.0:
            return price
    return None


def best_bid(book: Dict) -> Optional[float]:
    """Return an actually quoted bid; a midpoint is not executable liquidity."""
    bids = book.get("bids") or []
    for level in bids:
        price = _level_price(level)
        if price > 0.0 and _level_size(level) > 0.0:
            return price
    return None


def cross_book_imbalance(book_up: Dict, book_down: Dict) -> float:
    up_mid = book_up.get("mid")
    dn_mid = book_down.get("mid")
    skew = 0.0
    if up_mid is not None and dn_mid is not None:
        try:
            u, d = float(up_mid), float(dn_mid)
            if math.isfinite(u) and math.isfinite(d) and 0.0 < u < 1.0 and 0.0 < d < 1.0:
                skew = max(-1.0, min(1.0, (u - d) * 2.0))
        except (TypeError, ValueError):
            skew = 0.0

    imb_up = compute_book_imbalance(book_up.get("bids") or [], book_up.get("asks") or [])
    imb_dn = compute_book_imbalance(book_down.get("bids") or [], book_down.get("asks") or [])
    side = 0.5 * imb_up - 0.5 * imb_dn
    return max(-1.0, min(1.0, 0.55 * skew + 0.45 * side))


def build_features(
    market: Dict,
    book: Dict,
    external_momentum: float = 0.0,
    side: str = "up",
    book_other: Optional[Dict] = None,
    vs_open: float = 0.0,
) -> Dict:
    book = book if isinstance(book, dict) else {}
    raw_bids = book.get("bids", []) or []
    raw_asks = book.get("asks", []) or []
    bids = _valid_levels(raw_bids)
    asks = _valid_levels(raw_asks)
    invalid_level_count = len(raw_bids) + len(raw_asks) - len(bids) - len(asks)

    bid = bids[0]["price"] if bids else None
    ask = asks[0]["price"] if asks else None
    two_sided = bid is not None and ask is not None and bid < ask
    if not two_sided:
        # A one-sided, locked, or crossed book is not a trustworthy basis
        # for an executable quote or midpoint. Do not leak stale mid values
        # into downstream signal scoring.
        bid = None
        ask = None
        mid = None
    else:
        raw_mid = book.get("mid")
        try:
            candidate_mid = float(raw_mid)
        except (TypeError, ValueError, OverflowError):
            candidate_mid = float("nan")
        mid = candidate_mid if math.isfinite(candidate_mid) and 0.0 < candidate_mid < 1.0 else (bid + ask) / 2.0

    slug = market.get("slug") or market.get("market_slug") or ""
    question = market.get("question") or ""
    window_s = int(market.get("_window_seconds") or detect_window_seconds(slug, question))
    end_iso = market.get("endDate") or market.get("end_date_iso")
    tfeat = compute_time_features(end_iso, window_s)

    if book_other is not None:
        if side == "up":
            imbalance = cross_book_imbalance(book, book_other)
        else:
            imbalance = -cross_book_imbalance(book_other, book)
    else:
        imbalance = compute_book_imbalance(bids, asks)

    depth = compute_depth_usd(bids, asks)

    # Microprice weights each quote by liquidity resting on the opposite side.
    microprice = mid
    microprice_delta = 0.0
    if two_sided and mid is not None:
        bid_px, ask_px = _level_price(bids[0]), _level_price(asks[0])
        bid_sz, ask_sz = _level_size(bids[0]), _level_size(asks[0])
        total_sz = bid_sz + ask_sz
        if bid_px > 0 and ask_px > 0 and total_sz > 0:
            microprice = (ask_px * bid_sz + bid_px * ask_sz) / total_sz
            microprice_delta = max(-1.0, min(1.0, (microprice - mid) * 20.0))

    spread = 0.0
    if two_sided:
        spread = ask - bid

    if not two_sided:
        vol_regime = "unknown"
    elif spread > 0.04:
        vol_regime = "high"
    elif spread < 0.015:
        vol_regime = "low"
    else:
        vol_regime = "normal"

    fee_rate = parse_fee_schedule(market, default_rate=0.07)

    return {
        "book_valid": bool(two_sided),
        "book_invalid_level_count": invalid_level_count,
        "momentum": external_momentum,
        "vs_open": float(vs_open or 0.0),
        "book_imbalance": imbalance,
        "microprice": microprice,
        "microprice_delta": microprice_delta,
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
        "fee_rate": fee_rate,
        "fee_exponent": parse_fee_exponent(market),
        "fee_collection": market.get("_fee_collection", "usdc"),
        "fee_source": market.get("_fee_source") or ("gamma_fee_schedule" if market.get("feeSchedule") else "config_fallback"),
    }
