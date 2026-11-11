"""
Data layer for short-term crypto Up/Down markets on Polymarket.
Uses public Gamma API for discovery and CLOB for books.
"""
from typing import List, Dict, Optional, Any
import json
import logging
import requests

logger = logging.getLogger("astf.data")

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _parse_book_levels(levels: list) -> list:
    """Normalize bids/asks to list of {price, size} dicts."""
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
        self.session.headers.update({"User-Agent": "astf-bot/0.2"})
        logger.info("PolymarketData initialized (Gamma + CLOB)")

    def get_active_short_term_markets(self, assets: List[str] = None, limit: int = 100) -> List[Dict]:
        if assets is None:
            assets = ["btc", "eth"]
        try:
            params = {
                "active": "true",
                "closed": "false",
                "limit": limit,
                "order": "volume24hr",
                "ascending": "false",
            }
            r = self.session.get(f"{GAMMA}/markets", params=params, timeout=12)
            r.raise_for_status()
            markets = r.json()
            if not isinstance(markets, list):
                logger.warning("Unexpected Gamma response type")
                return []
        except Exception as e:
            logger.error(f"Gamma markets fetch failed: {e}")
            return []

        results = []
        asset_set = {a.lower() for a in assets}

        for m in markets:
            if not isinstance(m, dict):
                continue
            slug = (m.get("slug") or "").lower()
            question = (m.get("question") or "").lower()
            text = f"{slug} {question}"

            is_updown = ("updown" in slug or "up-or-down" in slug or "up or down" in question)
            is_short = any(x in text for x in ["5m", "15m", "5 min", "15 min", "5-minute", "15-minute"])
            is_asset = any(a in text for a in asset_set) or any(
                name in text for name in ["bitcoin", "ethereum"]
            )

            if not (is_updown and is_short and is_asset):
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
            m["_asset"] = next((a for a in asset_set if a in text), "btc")
            if "bitcoin" in text:
                m["_asset"] = "btc"
            elif "ethereum" in text:
                m["_asset"] = "eth"
            results.append(m)

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
