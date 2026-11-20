"""Causal, book-derived microstructure features for research and paper replay.

This module only describes observed L2 state and changes between two snapshots.
It does not emit trade recommendations or estimate outcome probabilities.
"""
import math


def _levels(rows, reverse=False):
    parsed = []
    for row in rows or []:
        try:
            price = float(row["price"])
            size = float(row["size"])
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        if math.isfinite(price) and math.isfinite(size) and 0 < price < 1 and size > 0:
            parsed.append((price, size))
    return sorted(parsed, key=lambda x: x[0], reverse=reverse)


def _depth(levels, count):
    return sum(size for _, size in levels[:count])


def _size_map(levels):
    return {price: size for price, size in levels}


def _withdrawal(previous, current, count):
    prior = _size_map(previous[:count])
    now = _size_map(current)
    removed = sum(max(0.0, size - now.get(price, 0.0)) for price, size in prior.items())
    return removed / max(sum(prior.values()), 1e-12)


def compute_microstructure_features(book, previous_book=None, *, depth_levels=5):
    """Return deterministic L2 features; incomplete/crossed books fail closed."""
    if not isinstance(book, dict) or not book.get("complete", True):
        return {"valid": False, "reason": "incomplete_book"}
    n = max(1, int(depth_levels))
    bids = _levels(book.get("bids"), reverse=True)
    asks = _levels(book.get("asks"))
    if not bids or not asks:
        return {"valid": False, "reason": "one_sided_book"}
    bid, bid_size = bids[0]
    ask, ask_size = asks[0]
    if bid >= ask:
        return {"valid": False, "reason": "crossed_or_locked_book"}

    mid = (bid + ask) / 2.0
    spread = ask - bid
    total_top = bid_size + ask_size
    microprice = ((ask * bid_size) + (bid * ask_size)) / total_top if total_top > 0 else mid
    bid_depth = _depth(bids, n)
    ask_depth = _depth(asks, n)
    total_depth = bid_depth + ask_depth
    result = {
        "valid": True,
        "best_bid": bid,
        "best_ask": ask,
        "mid": mid,
        "spread": spread,
        "spread_bps": spread / mid * 10000.0,
        "microprice": microprice,
        "microprice_edge_bps": (microprice - mid) / mid * 10000.0,
        "top_imbalance": (bid_size - ask_size) / total_top if total_top else 0.0,
        "bid_depth": bid_depth,
        "ask_depth": ask_depth,
        "depth_imbalance": (bid_depth - ask_depth) / total_depth if total_depth else 0.0,
        "ofi": None,
        "ofi_normalized": None,
        "bid_withdrawal_fraction": None,
        "ask_withdrawal_fraction": None,
        "withdrawal_imbalance": None,
    }

    if isinstance(previous_book, dict) and previous_book.get("complete", True):
        prev_bids = _levels(previous_book.get("bids"), reverse=True)
        prev_asks = _levels(previous_book.get("asks"))
        if prev_bids and prev_asks:
            prev_bid_map = _size_map(prev_bids[:n])
            prev_ask_map = _size_map(prev_asks[:n])
            curr_bid_map = _size_map(bids[:n])
            curr_ask_map = _size_map(asks[:n])
            bid_delta = sum(curr_bid_map.get(p, 0.0) - q for p, q in prev_bid_map.items())
            ask_delta = sum(curr_ask_map.get(p, 0.0) - q for p, q in prev_ask_map.items())
            prev_depth = sum(prev_bid_map.values()) + sum(prev_ask_map.values())
            ofi = bid_delta - ask_delta
            bid_withdrawal = _withdrawal(prev_bids, bids, n)
            ask_withdrawal = _withdrawal(prev_asks, asks, n)
            result.update({
                "ofi": ofi,
                "ofi_normalized": ofi / max(prev_depth, 1e-12),
                "bid_withdrawal_fraction": bid_withdrawal,
                "ask_withdrawal_fraction": ask_withdrawal,
                "withdrawal_imbalance": ask_withdrawal - bid_withdrawal,
            })
    return result
