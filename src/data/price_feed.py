"""
External price feed with multi-timeframe momentum.

Sources:
- Binance spot mid (fast cache)
- Binance klines for 1m / 5m / 15m returns
- Optional local mid history blend from the bot loop
"""
from typing import Dict, Optional, List
import logging
import time
import requests

logger = logging.getLogger("astf.price")

SYMBOLS = {"btc": "BTCUSDT", "eth": "ETHUSDT"}


class PriceFeed:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "astf-bot/0.3"})
        self.cache: Dict[str, float] = {}
        self.last_fetch = 0.0
        self.cache_ttl = 6.0
        self._mtf_cache: Dict[str, tuple] = {}  # asset -> (ts, momentum_dict)
        self._mtf_ttl = 25.0

    def _fetch_binance_price(self, symbol: str) -> Optional[float]:
        try:
            r = self.session.get(
                "https://api.binance.com/api/v3/ticker/price",
                params={"symbol": symbol},
                timeout=5,
            )
            r.raise_for_status()
            return float(r.json()["price"])
        except Exception as e:
            logger.debug(f"Binance price failed for {symbol}: {e}")
            return None

    def _fetch_klines(self, symbol: str, interval: str = "1m", limit: int = 20) -> List[float]:
        """Return list of close prices (oldest → newest)."""
        try:
            r = self.session.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": symbol, "interval": interval, "limit": limit},
                timeout=6,
            )
            r.raise_for_status()
            data = r.json()
            return [float(c[4]) for c in data]  # close
        except Exception as e:
            logger.debug(f"Klines failed {symbol} {interval}: {e}")
            return []

    def get_prices(self) -> Dict[str, float]:
        now = time.time()
        if now - self.last_fetch < self.cache_ttl and self.cache:
            return self.cache

        for asset, symbol in SYMBOLS.items():
            p = self._fetch_binance_price(symbol)
            if p is not None:
                self.cache[asset] = p
        self.last_fetch = now
        return self.cache

    @staticmethod
    def _ret(prices: List[float], bars: int) -> float:
        if len(prices) <= bars or prices[-1 - bars] <= 0:
            return 0.0
        return (prices[-1] - prices[-1 - bars]) / prices[-1 - bars]

    def multi_timeframe_momentum(self, asset: str) -> Dict[str, float]:
        """
        Returns {
          m1:  ~1–3 minute momentum scaled [-1,1]
          m5:  ~5 minute
          m15: ~15 minute
          combined: weighted blend
        }
        """
        now = time.time()
        if asset in self._mtf_cache:
            ts, val = self._mtf_cache[asset]
            if now - ts < self._mtf_ttl:
                return val

        symbol = SYMBOLS.get(asset)
        if not symbol:
            return {"m1": 0.0, "m5": 0.0, "m15": 0.0, "combined": 0.0}

        closes = self._fetch_klines(symbol, interval="1m", limit=20)
        if len(closes) < 5:
            out = {"m1": 0.0, "m5": 0.0, "m15": 0.0, "combined": 0.0}
            self._mtf_cache[asset] = (now, out)
            return out

        # Scale: ~0.4% move ≈ 1.0 on short TF; longer TF less sensitive
        r1 = self._ret(closes, 2)   # ~2 min
        r5 = self._ret(closes, 5)   # ~5 min
        r15 = self._ret(closes, min(15, len(closes) - 1))

        m1 = max(-1.0, min(1.0, r1 * 120))
        m5 = max(-1.0, min(1.0, r5 * 60))
        m15 = max(-1.0, min(1.0, r15 * 40))

        # Weight short TF more for 5m/15m prediction markets
        combined = 0.45 * m1 + 0.35 * m5 + 0.20 * m15
        combined = max(-1.0, min(1.0, combined))

        out = {"m1": m1, "m5": m5, "m15": m15, "combined": combined}
        self._mtf_cache[asset] = (now, out)
        return out

    def momentum(self, asset: str, lookback_prices: list = None) -> float:
        """
        Primary momentum used by the bot: multi-timeframe combined,
        optionally blended with local loop history.
        """
        mtf = self.multi_timeframe_momentum(asset)
        combined = mtf["combined"]

        if lookback_prices and len(lookback_prices) >= 3 and lookback_prices[0] > 0:
            local_ret = (lookback_prices[-1] - lookback_prices[0]) / lookback_prices[0]
            local = max(-1.0, min(1.0, local_ret * 80))
            combined = 0.7 * combined + 0.3 * local

        return float(max(-1.0, min(1.0, combined)))
