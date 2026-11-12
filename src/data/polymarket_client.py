"""
Data layer for short-term crypto Up/Down markets on Polymarket.
Uses public Gamma API for discovery and CLOB for books.
"""
from typing import List, Dict, Optional, Any
import json
import logging
import time
import requests

logger = logging.getLogger("astf.data")

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

# Official recurring series on Polymarket
SERIES_BY_ASSET = {
    "btc": ["btc-up-or-down-5m", "btc-up-or-down-15m"],
    "eth": ["eth-up-or-down-5m", "eth-up-or-down-15m"],
}


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


class PolymarketData:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "astf-bot/0.3"})
        logger.info("PolymarketData initialized (Gamma series + CLOB)")

    def _series_events(self, series_slug: str) -> List[Dict]:
        try:
            r = self.session.get(
                f"{GAMMA}/series",
                params={"slug": series_slug},
                timeout=12,
            )
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list) or not data:
                return []
            return data[0].get("events") or []
        except Exception as e:
            logger.warning(f"Series fetch failed for {series_slug}: {e}")
            return []

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

    def get_active_short_term_markets(self, assets: List[str] = None, limit: int = 100) -> List[Dict]:
        """
        Discover live 5m/15m Up/Down windows via series endpoints.

        Why not /markets?order=volume24hr:
        short windows are tiny individual markets and almost never appear
        in the global top-N volume list, so the old filter returned [].
        """
        if assets is None:
            assets = ["btc", "eth"]

        now = time.time()
        results = []
        seen = set()

        for asset in assets:
            asset = asset.lower()
            for series_slug in SERIES_BY_ASSET.get(asset, []):
                events = self._series_events(series_slug)
                # Prefer events ending soon (live / next windows)
                ranked = []
                for e in events:
                    if not isinstance(e, dict):
                        continue
                    if e.get("closed"):
                        continue
                    end = e.get("endDate") or ""
                    ranked.append(e)
                # Keep a modest number per series
                for e in ranked[:12]:
                    slug = e.get("slug") or e.get("ticker")
                    if not slug or slug in seen:
                        continue
                    m = self._market_by_slug(slug)
                    if not m:
                        continue
                    if m.get("closed") or not m.get("active", True):
                        continue
                    if not m.get("acceptingOrders", True) and m.get("enableOrderBook") is False:
                        continue

                    token_ids = m.get("clobTokenIds")
                    if isinstance(token_ids, str):
                        try:
                            token_ids = json.loads(token_ids)
                        except Exception:
                            token_ids = []
                    if not isinstance(token_ids, list) or len(token_ids) < 2:
                        continue

                    # Skip already-ended windows if endDate is in the past
                    end_iso = m.get("endDate") or e.get("endDate")
                    if end_iso:
                        try:
                            # rough parse: if clearly past, skip
                            from datetime import datetime, timezone
                            end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
                            if end_dt.timestamp() < now - 30:
                                continue
                        except Exception:
                            pass

                    m["_up_token"] = str(token_ids[0])
                    m["_down_token"] = str(token_ids[1])
                    m["_asset"] = asset
                    m["_series"] = series_slug
                    if not m.get("slug"):
                        m["slug"] = slug
                    seen.add(slug)
                    results.append(m)
                    if len(results) >= limit:
                        logger.info(f"Found {len(results)} active short-term crypto markets")
                        return results

        logger.info(f"Found {len(results)} active short-term crypto markets")
        return results

    def get_order_book(self, token_id: str) -> Dict:
        try:
            r = self.session.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=8)
            r.raise_for_status()
            data = r.json()
            bids = _parse_book_levels(data.get("bids", []))
            asks = _parse_book_levels(data.get("asks", []))
            mid = None
            if bids and asks:
                mid = (bids[0]["price"] + asks[0]["price"]) / 2
            return {"bids": bids, "asks": asks, "mid": mid, "raw": data}
        except Exception as e:
            tid = (token_id or "")[:12]
            logger.warning(f"Book fetch failed for {tid}...: {e}")
            return {"bids": [], "asks": [], "mid": None}

    def get_mid_price(self, token_id: str) -> Optional[float]:
        return self.get_order_book(token_id).get("mid")
