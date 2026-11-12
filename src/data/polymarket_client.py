"""
Data layer for short-term crypto Up/Down markets on Polymarket.
Uses public Gamma API for discovery and CLOB for books.

IMPORTANT: Gamma /series for btc-up-or-down-5m often returns only far-future
windows (many hours out). Live windows use deterministic slugs:
  {asset}-updown-{5m|15m}-{unix_aligned_to_window}
"""
from typing import List, Dict, Optional, Any
import json
import logging
import time
from datetime import datetime, timezone
import requests

logger = logging.getLogger("astf.data")

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

# slug pattern: btc-updown-5m-{ts}
WINDOWS = [
    ("5m", 300),
    ("15m", 900),
]
ASSETS = ("btc", "eth")


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _parse_book_levels(levels: list) -> list:
    out = []
    for lvl in levels or []:
        if isinstance(lvl, dict):
            out.append({"price": _safe_float(lvl.get("price")), "size": _safe_float(lvl.get("size"))})
        elif isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
            out.append({"price": _safe_float(lvl[0]), "size": _safe_float(lvl[1])})
    return out


def _end_ts(iso: Optional[str]) -> float:
    if not iso:
        return float("inf")
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except Exception:
        return float("inf")


class PolymarketData:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "astf-bot/0.5"})
        logger.info("PolymarketData initialized (slug-time discovery + CLOB)")

    def _market_by_slug(self, slug: str) -> Optional[Dict]:
        try:
            r = self.session.get(
                f"{GAMMA}/markets",
                params={"slug": slug},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list) and data:
                return data[0]
            if isinstance(data, dict) and data.get("slug"):
                return data
        except Exception as e:
            logger.debug(f"Market slug fetch failed {slug}: {e}")
        return None

    def _candidate_slugs(self, assets: List[str], now: float) -> List[tuple]:
        """
        Build (asset, window_label, slug, window_sec) for nearby live windows.
        Slug unix = start of window (aligned).
        """
        out = []
        now_i = int(now)
        for asset in assets:
            asset = asset.lower()
            if asset not in ASSETS:
                continue
            for label, step in WINDOWS:
                base = (now_i // step) * step
                # previous (may still be resolving), current, next few
                for i in range(-1, 5):
                    ts = base + i * step
                    slug = f"{asset}-updown-{label}-{ts}"
                    out.append((asset, label, slug, step, ts))
        return out

    def get_active_short_term_markets(self, assets: List[str] = None, limit: int = 100) -> List[Dict]:
        if assets is None:
            assets = ["btc", "eth"]

        now = time.time()
        results = []
        seen = set()

        for asset, label, slug, step, win_start in self._candidate_slugs(assets, now):
            if slug in seen:
                continue
            m = self._market_by_slug(slug)
            if not m:
                continue
            if m.get("closed") or not m.get("active", True):
                continue

            end_iso = m.get("endDate")
            end_t = _end_ts(end_iso)
            # Prefer endDate; fallback to window start + step
            if end_t == float("inf"):
                end_t = float(win_start + step)

            # Must still have meaningful time left (skip ending in <40s)
            if end_t < now + 40:
                continue
            # Safety: ignore absurd far windows
            if end_t > now + 2 * 3600:
                continue

            token_ids = m.get("clobTokenIds")
            if isinstance(token_ids, str):
                try:
                    token_ids = json.loads(token_ids)
                except Exception:
                    token_ids = []
            if not isinstance(token_ids, list) or len(token_ids) < 2:
                continue

            m["_up_token"] = str(token_ids[0])
            m["_down_token"] = str(token_ids[1])
            m["_asset"] = asset
            m["_series"] = f"{asset}-up-or-down-{label}"
            m["_end_ts"] = end_t
            m["_window"] = label
            if not m.get("slug"):
                m["slug"] = slug
            if end_iso and not m.get("endDate"):
                m["endDate"] = end_iso
            elif not m.get("endDate"):
                # synthesize ISO if missing
                m["endDate"] = datetime.fromtimestamp(end_t, tz=timezone.utc).isoformat().replace("+00:00", "Z")

            seen.add(slug)
            results.append(m)

        results.sort(key=lambda x: x.get("_end_ts", float("inf")))
        results = results[:limit]
        logger.info(f"Found {len(results)} active short-term crypto markets")
        if not results:
            logger.warning(
                "Zero markets — check Gamma reachability / slug pattern "
                "(btc-updown-5m-{unix})"
            )
        return results

    def get_order_book(self, token_id: str) -> Dict:
        try:
            r = self.session.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=8)
            r.raise_for_status()
            data = r.json()
            bids = _parse_book_levels(data.get("bids", []))
            asks = _parse_book_levels(data.get("asks", []))
            bids = sorted(bids, key=lambda x: x["price"], reverse=True)
            asks = sorted(asks, key=lambda x: x["price"])
            mid = None
            if bids and asks:
                mid = (bids[0]["price"] + asks[0]["price"]) / 2
            return {
                "bids": bids,
                "asks": asks,
                "mid": mid,
                "best_bid": bids[0]["price"] if bids else None,
                "best_ask": asks[0]["price"] if asks else None,
                "raw": data,
            }
        except Exception as e:
            tid = (token_id or "")[:12]
            logger.warning(f"Book fetch failed for {tid}...: {e}")
            return {"bids": [], "asks": [], "mid": None, "best_bid": None, "best_ask": None}

    def get_mid_price(self, token_id: str) -> Optional[float]:
        return self.get_order_book(token_id).get("mid")
