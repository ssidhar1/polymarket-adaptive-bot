"""
Data layer for short-term crypto Up/Down markets on Polymarket.
Uses public Gamma API for discovery and CLOB for books.
"""
from typing import List, Dict, Optional
import logging
import requests

logger = logging.getLogger("astf.data")

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

class PolymarketData:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "astf-bot/0.1"})
        logger.info("PolymarketData initialized (Gamma + CLOB)")

    def get_active_short_term_markets(self, assets: List[str] = ["btc", "eth"], limit: int = 80) -> List[Dict]:
        """
        Discover currently active 5m / 15m crypto Up/Down markets.
        Strategy: pull recent active markets and filter by slug/question patterns.
        """
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
        except Exception as e:
            logger.error(f"Gamma markets fetch failed: {e}")
            return []

        results = []
        asset_set = {a.lower() for a in assets}

        for m in markets:
            slug = (m.get("slug") or "").lower()
            question = (m.get("question") or "").lower()
            text = slug + " " + question

            # Must look like short-term up/down
            is_updown = ("updown" in slug or "up-or-down" in slug or "up or down" in question)
            is_short = any(x in text for x in ["5m", "15m", "5 min", "15 min", "5-minute", "15-minute"])
            is_asset = any(a in text for a in asset_set) or any(a in text for a in ["bitcoin", "ethereum"])

            if not (is_updown and is_short and is_asset):
                continue

            # Normalize token ids
            token_ids = m.get("clobTokenIds")
            if isinstance(token_ids, str):
                import json
                try:
                    token_ids = json.loads(token_ids)
                except Exception:
                    token_ids = []

            if not token_ids or len(token_ids) < 2:
                continue

            m["_up_token"] = token_ids[0]
            m["_down_token"] = token_ids[1]
            m["_asset"] = next((a for a in asset_set if a in text), "btc")
            results.append(m)

        logger.info(f"Found {len(results)} active short-term crypto markets")
        return results

    def get_order_book(self, token_id: str) -> Dict:
        """Fetch L2 book for a token from CLOB."""
        try:
            r = self.session.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=8)
            r.raise_for_status()
            data = r.json()
            bids = data.get("bids", []) or []
            asks = data.get("asks", []) or []
            mid = None
            if bids and asks:
                mid = (float(bids[0]["price"]) + float(asks[0]["price"])) / 2
            return {"bids": bids, "asks": asks, "mid": mid, "raw": data}
        except Exception as e:
            logger.warning(f"Book fetch failed for {token_id[:12]}...: {e}")
            return {"bids": [], "asks": [], "mid": None}

    def get_mid_price(self, token_id: str) -> Optional[float]:
        return self.get_order_book(token_id).get("mid")
