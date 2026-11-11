"""
Lightweight external price feed for momentum.
Uses public Binance or Coinbase mid as proxy (Chainlink is preferred for settlement but harder to query free).
"""
from typing import Dict, Optional
import logging
import time
import requests

logger = logging.getLogger("astf.price")

class PriceFeed:
    def __init__(self):
        self.session = requests.Session()
        self.cache: Dict[str, float] = {}
        self.last_fetch = 0.0
        self.cache_ttl = 8  # seconds

    def _fetch_binance(self, symbol: str) -> Optional[float]:
        try:
            r = self.session.get(
                "https://api.binance.com/api/v3/ticker/price",
                params={"symbol": symbol},
                timeout=5,
            )
            r.raise_for_status()
            return float(r.json()["price"])
        except Exception as e:
            logger.debug(f"Binance fetch failed for {symbol}: {e}")
            return None

    def get_prices(self) -> Dict[str, float]:
        now = time.time()
        if now - self.last_fetch < self.cache_ttl and self.cache:
            return self.cache

        btc = self._fetch_binance("BTCUSDT")
        eth = self._fetch_binance("ETHUSDT")
        if btc:
            self.cache["btc"] = btc
        if eth:
            self.cache["eth"] = eth
        self.last_fetch = now
        return self.cache

    def momentum(self, asset: str, lookback_prices: list) -> float:
        """
        Very simple momentum: recent return scaled to [-1, 1].
        lookback_prices should be recent mids (newest last).
        """
        if len(lookback_prices) < 2:
            return 0.0
        ret = (lookback_prices[-1] - lookback_prices[0]) / lookback_prices[0]
        # clip and scale
        return max(-1.0, min(1.0, ret * 50))  # 2% move → ~1.0
