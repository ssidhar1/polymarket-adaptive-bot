"""
Check whether a market has resolved and return the final price of a token.
"""
from typing import Optional, Dict, Tuple
import logging
import json
import requests

logger = logging.getLogger("astf.resolution")
GAMMA = "https://gamma-api.polymarket.com"

class ResolutionChecker:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "astf-bot/0.1"})

    def check(self, market_id: str = None, slug: str = None) -> Tuple[bool, Optional[float], Optional[float]]:
        """
        Returns (is_closed, up_price, down_price).
        Prices are 0 or 1 (or last mid if still open).
        """
        try:
            if slug:
                r = self.session.get(f"{GAMMA}/markets", params={"slug": slug}, timeout=8)
            elif market_id:
                r = self.session.get(f"{GAMMA}/markets/{market_id}", timeout=8)
            else:
                return False, None, None

            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                if not data:
                    return False, None, None
                m = data[0]
            else:
                m = data

            closed = bool(m.get("closed"))
            outcome_prices = m.get("outcomePrices")
            if isinstance(outcome_prices, str):
                try:
                    outcome_prices = json.loads(outcome_prices)
                except Exception:
                    outcome_prices = None

            up_p = down_p = None
            if outcome_prices and len(outcome_prices) >= 2:
                up_p = float(outcome_prices[0])
                down_p = float(outcome_prices[1])

            return closed, up_p, down_p
        except Exception as e:
            logger.debug(f"Resolution check failed: {e}")
            return False, None, None

    def token_final_price(self, direction: str, up_price: Optional[float], down_price: Optional[float]) -> Optional[float]:
        if up_price is None or down_price is None:
            return None
        return up_price if direction == "up" else down_price
