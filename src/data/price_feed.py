"""
External price feed with multi-timeframe momentum and bounded outage backoff.
"""
from typing import Dict, Optional, List
import logging
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("astf.price")

SYMBOLS = {"btc": "BTCUSDT", "eth": "ETHUSDT"}


class PriceFeed:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "astf-bot/0.7"})
        retry = Retry(
            total=2,
            backoff_factor=0.3,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET"]),
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
        self.session.mount("https://", adapter)
        self.cache: Dict[str, float] = {}
        self.cache_updated_at: Dict[str, float] = {}
        self.max_stale_seconds = 15.0
        self.last_fetch = 0.0
        self.cache_ttl = 6.0
        self._mtf_cache: Dict[str, tuple] = {}
        self._mtf_ttl = 25.0
        self._fail_count = 0
        self._next_retry_at = 0.0

    def _fetch_binance_price(self, symbol: str) -> Optional[float]:
        try:
            r = self.session.get(
                "https://api.binance.com/api/v3/ticker/price",
                params={"symbol": symbol},
                timeout=4,
            )
            r.raise_for_status()
            data = r.json()
            p = float(data["price"])
            if p <= 0:
                return None
            return p
        except Exception as e:
            logger.debug("Binance price failed for %s: %s", symbol, e)
            return None

    def _fetch_klines(self, symbol: str, interval: str = "1m", limit: int = 20) -> List[float]:
        try:
            r = self.session.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": symbol, "interval": interval, "limit": limit},
                timeout=5,
            )
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list):
                return []
            out = []
            for c in data:
                try:
                    out.append(float(c[4]))
                except (TypeError, ValueError, IndexError):
                    continue
            return out
        except Exception as e:
            logger.debug("Klines failed %s %s: %s", symbol, interval, e)
            return []

    def _fresh_cache(self, now):
        return {
            asset: price for asset, price in self.cache.items()
            if 0 <= now - self.cache_updated_at.get(asset, 0.0) <= self.max_stale_seconds
        }

    def get_prices(self) -> Dict[str, float]:
        now = time.time()
        if now - self.last_fetch < self.cache_ttl and self.cache:
            return self._fresh_cache(now)
        # Back off after a failed/partial poll. A stale cache is never promoted
        # to fresh; the strategy's get_fresh_price gate remains authoritative.
        if now < self._next_retry_at:
            return self._fresh_cache(now)

        successes = 0
        for asset, symbol in SYMBOLS.items():
            p = self._fetch_binance_price(symbol)
            if p is not None:
                self.cache[asset] = p
                self.cache_updated_at[asset] = now
                successes += 1

        self.last_fetch = now
        if successes == len(SYMBOLS):
            self._fail_count = 0
            self._next_retry_at = 0.0
        else:
            self._fail_count += 1
            delay = min(30.0, 2.0 ** min(self._fail_count, 5))
            self._next_retry_at = now + delay
            if self._fail_count in (1, 3, 5) or self._fail_count % 10 == 0:
                logger.warning(
                    "Binance price feed partial/unavailable (failures=%d, successes=%d/%d); "
                    "retry in %.0fs; only cache <=%.0fs old is exposed",
                    self._fail_count, successes, len(SYMBOLS), delay, self.max_stale_seconds,
                )
        return self._fresh_cache(now)

    def get_fresh_price(self, asset: str, max_age: float = None) -> Optional[float]:
        now = time.time()
        limit = self.max_stale_seconds if max_age is None else max(0.0, float(max_age))
        updated = self.cache_updated_at.get(asset, 0.0)
        if not updated or now - updated < 0 or now - updated > limit:
            return None
        return self.cache.get(asset)

    def health(self) -> dict:
        """Small diagnostic snapshot; age is reported, never used to refresh data."""
        now = time.time()
        return {
            "failures": self._fail_count,
            "retry_in_seconds": max(0.0, self._next_retry_at - now),
            "prices": {
                asset: {
                    "age_seconds": max(0.0, now - self.cache_updated_at[asset]),
                    "fresh": now - self.cache_updated_at[asset] <= self.max_stale_seconds,
                }
                for asset in self.cache
            },
        }

    @staticmethod
    def _ret(prices: List[float], bars: int) -> float:
        if len(prices) <= bars or prices[-1 - bars] <= 0:
            return 0.0
        return (prices[-1] - prices[-1 - bars]) / prices[-1 - bars]

    def multi_timeframe_momentum(self, asset: str) -> Dict[str, float]:
        now = time.time()
        if asset in self._mtf_cache:
            ts, val = self._mtf_cache[asset]
            if now - ts < self._mtf_ttl:
                return val

        zero = {"m1": 0.0, "m5": 0.0, "m15": 0.0, "combined": 0.0}
        symbol = SYMBOLS.get(asset)
        if not symbol:
            return zero

        closes = self._fetch_klines(symbol, interval="1m", limit=20)
        if len(closes) < 5:
            # Never reuse stale momentum when the upstream candle feed fails.
            self._mtf_cache[asset] = (now, zero)
            return zero

        r1 = self._ret(closes, 2)
        r5 = self._ret(closes, 5)
        r15 = self._ret(closes, min(15, len(closes) - 1))
        m1 = max(-1.0, min(1.0, r1 * 120))
        m5 = max(-1.0, min(1.0, r5 * 60))
        m15 = max(-1.0, min(1.0, r15 * 40))
        combined = max(-1.0, min(1.0, 0.45 * m1 + 0.35 * m5 + 0.20 * m15))
        out = {"m1": m1, "m5": m5, "m15": m15, "combined": combined}
        self._mtf_cache[asset] = (now, out)
        return out

    def momentum(self, asset: str, lookback_prices: list = None) -> float:
        mtf = self.multi_timeframe_momentum(asset)
        combined = mtf["combined"]
        if lookback_prices and len(lookback_prices) >= 3:
            try:
                a, b = float(lookback_prices[0]), float(lookback_prices[-1])
                if a > 0 and b > 0:
                    local_ret = (b - a) / a
                    local = max(-1.0, min(1.0, local_ret * 80))
                    combined = 0.7 * combined + 0.3 * local
            except (TypeError, ValueError):
                pass
        return float(max(-1.0, min(1.0, combined)))
