"""Causal, book-derived microstructure features for research and paper replay.

This module describes observed L2 state and changes between two snapshots.
It does not emit trade recommendations or infer whether disappearing size
was canceled versus executed; it deliberately labels that measure as depth
disappearance rather than liquidity withdrawal.
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


def _depth_disappearance(previous, current, count):
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
        "valid": True, "best_bid": bid, "best_ask": ask, "mid": mid,
        "spread": spread, "spread_bps": spread / mid * 10000.0,
        "microprice": microprice,
        "microprice_edge_bps": (microprice - mid) / mid * 10000.0,
        "top_imbalance": (bid_size - ask_size) / total_top if total_top else 0.0,
        "bid_depth": bid_depth, "ask_depth": ask_depth,
        "depth_imbalance": (bid_depth - ask_depth) / total_depth if total_depth else 0.0,
        "ofi": None, "ofi_normalized": None, "depth_delta": None,
        "depth_delta_normalized": None,
        "bid_depth_disappearance_fraction": None,
        "ask_depth_disappearance_fraction": None,
        "depth_disappearance_imbalance": None,
    }

    if isinstance(previous_book, dict) and previous_book.get("complete", True):
        prev_bids = _levels(previous_book.get("bids"), reverse=True)
        prev_asks = _levels(previous_book.get("asks"))
        if prev_bids and prev_asks:
            prev_bid, prev_bid_size = prev_bids[0]
            prev_ask, prev_ask_size = prev_asks[0]

            # Cont-Kukanov-Stoikov best-queue OFI across consecutive snapshots.
            if bid > prev_bid:
                bid_flow = bid_size
            elif bid == prev_bid:
                bid_flow = bid_size - prev_bid_size
            else:
                bid_flow = -prev_bid_size

            if ask < prev_ask:
                ask_flow = -ask_size
            elif ask == prev_ask:
                ask_flow = prev_ask_size - ask_size
            else:
                ask_flow = prev_ask_size

            ofi = bid_flow + ask_flow
            prev_top_depth = prev_bid_size + prev_ask_size

            prev_bid_map = _size_map(prev_bids[:n])
            prev_ask_map = _size_map(prev_asks[:n])
            curr_bid_map = _size_map(bids[:n])
            curr_ask_map = _size_map(asks[:n])
            bid_prices = set(prev_bid_map) | set(curr_bid_map)
            ask_prices = set(prev_ask_map) | set(curr_ask_map)
            bid_delta = sum(curr_bid_map.get(p, 0.0) - prev_bid_map.get(p, 0.0) for p in bid_prices)
            ask_delta = sum(curr_ask_map.get(p, 0.0) - prev_ask_map.get(p, 0.0) for p in ask_prices)
            depth_delta = bid_delta - ask_delta
            prev_depth = sum(prev_bid_map.values()) + sum(prev_ask_map.values())
            bid_disappearance = _depth_disappearance(prev_bids, bids, n)
            ask_disappearance = _depth_disappearance(prev_asks, asks, n)
            result.update({
                "ofi": ofi,
                "ofi_normalized": ofi / max(prev_top_depth, 1e-12),
                "depth_delta": depth_delta,
                "depth_delta_normalized": depth_delta / max(prev_depth, 1e-12),
                "bid_depth_disappearance_fraction": bid_disappearance,
                "ask_depth_disappearance_fraction": ask_disappearance,
                "depth_disappearance_imbalance": ask_disappearance - bid_disappearance,
            })
    return result
