"""
External price feed with multi-timeframe momentum and bounded outage backoff.
Falls back to Coinbase and Kraken when Binance is unavailable.
"""
from typing import Dict, Optional, List
import logging
import math
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("astf.price")


# Chainlink AggregatorV3 on Ethereum mainnet (latestRoundData selector)
CHAINLINK_FEEDS = {
    "btc": "0xF4030086522a5bEEa4988F8cA5B36dbC97BeE88c",
    "eth": "0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419",
}
CHAINLINK_RPCS = [
    "https://eth.llamarpc.com",
    "https://ethereum.publicnode.com",
    "https://rpc.ankr.com/eth",
]
CHAINLINK_SELECTOR = "0xfeaf968c"  # latestRoundData()

SYMBOLS = {"btc": "BTCUSDT", "eth": "ETHUSDT"}


def _positive_finite(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0.0 else None


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
        # Rolling prices for 60s average (Chainlink-style settlement proxy)
        self._price_ring: Dict[str, list] = {"btc": [], "eth": []}
        self._ring_max = 12  # ~60s at 5s polls


    def _fetch_chainlink_price(self, asset: str) -> Optional[float]:
        """Read latest Chainlink AggregatorV3 price via public Ethereum RPC eth_call."""
        addr = CHAINLINK_FEEDS.get(asset)
        if not addr:
            return None
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": addr, "data": CHAINLINK_SELECTOR}, "latest"],
        }
        for rpc in CHAINLINK_RPCS:
            try:
                r = self.session.post(rpc, json=payload, timeout=5)
                r.raise_for_status()
                result = r.json().get("result")
                if not result or not isinstance(result, str) or len(result) < 130:
                    continue
                # answer is the second 32-byte word (offset 66..130)
                answer_hex = result[66:130]
                answer = int(answer_hex, 16)
                # signed int256
                if answer >= 2**255:
                    answer -= 2**256
                price = answer / 1e8  # BTC/USD and ETH/USD use 8 decimals
                return _positive_finite(price)
            except Exception as e:
                logger.debug("Chainlink %s via %s failed: %s", asset, rpc, e)
                continue
        return None

    def _fetch_binance_price(self, symbol: str) -> Optional[float]:
        try:
            r = self.session.get(
                "https://api.binance.com/api/v3/ticker/price",
                params={"symbol": symbol},
                timeout=4,
            )
            r.raise_for_status()
            data = r.json()
            return _positive_finite(data["price"])
        except Exception as e:
            logger.debug("Binance price failed for %s: %s", symbol, e)
            return None

    def _fetch_coinbase_price(self, asset: str) -> Optional[float]:
        pair = {"btc": "BTC-USD", "eth": "ETH-USD"}.get(asset)
        if not pair:
            return None
        try:
            r = self.session.get(
                f"https://api.coinbase.com/v2/prices/{pair}/spot",
                timeout=4,
            )
            r.raise_for_status()
            data = r.json()
            return _positive_finite(data.get("data", {}).get("amount"))
        except Exception as e:
            logger.debug("Coinbase price failed for %s: %s", asset, e)
            return None

    def _fetch_kraken_price(self, asset: str) -> Optional[float]:
        pair = {"btc": "XBTUSD", "eth": "ETHUSD"}.get(asset)
        if not pair:
            return None
        try:
            r = self.session.get(
                "https://api.kraken.com/0/public/Ticker",
                params={"pair": pair},
                timeout=4,
            )
            r.raise_for_status()
            data = r.json()
            result = data.get("result", {})
            key = next(iter(result), None)
            if key and result[key].get("c"):
                return _positive_finite(result[key]["c"][0])
            return None
        except Exception as e:
            logger.debug("Kraken price failed for %s: %s", asset, e)
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
                    close = _positive_finite(c[4])
                    if close is not None:
                        out.append(close)
                except (TypeError, ValueError, IndexError):
                    continue
            return out
        except Exception as e:
            logger.debug("Klines failed %s %s: %s", symbol, interval, e)
            return []

    def _fresh_cache(self, now):
        fresh = {}
        for asset, raw_price in self.cache.items():
            price = _positive_finite(raw_price)
            try:
                updated = float(self.cache_updated_at.get(asset, 0.0))
            except (TypeError, ValueError, OverflowError):
                continue
            if (price is not None and math.isfinite(updated)
                    and 0 <= now - updated <= self.max_stale_seconds):
                fresh[asset] = price
        return fresh

    def get_60s_avg(self, asset: str) -> Optional[float]:
        """Return a simple 60s average from recent polls. Proxy for Chainlink settlement input."""
        ring = self._price_ring.get(asset, [])
        if len(ring) < 3:
            return None
        vals = [p for _, p in ring[-self._ring_max:]]
        return sum(vals) / len(vals) if vals else None

    def get_prices(self) -> Dict[str, float]:
        now = time.time()
        if now - self.last_fetch < self.cache_ttl and self.cache:
            return self._fresh_cache(now)
        if now < self._next_retry_at:
            return self._fresh_cache(now)

        successes = 0
        for asset, symbol in SYMBOLS.items():
            p = self._fetch_chainlink_price(asset)
            src = "chainlink" if p is not None else None
            if p is None:
                p = _positive_finite(self._fetch_binance_price(symbol))
                src = "binance" if p is not None else None
            if p is None:
                p = self._fetch_coinbase_price(asset)
                src = "coinbase" if p is not None else None
            if p is None:
                p = self._fetch_kraken_price(asset)
                src = "kraken" if p is not None else None
            if p is not None and src:
                self._last_source = getattr(self, "_last_source", {})
                self._last_source[asset] = src
            if p is not None:
                self.cache[asset] = p
                self.cache_updated_at[asset] = now
                successes += 1
                ring = self._price_ring.setdefault(asset, [])
                ring.append((now, p))
                if len(ring) > self._ring_max:
                    ring.pop(0)

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
                    "Price feed partial/unavailable (failures=%d, successes=%d/%d); "
                    "retry in %.0fs; only cache <=%.0fs old is exposed",
                    self._fail_count, successes, len(SYMBOLS), delay, self.max_stale_seconds,
                )
        return self._fresh_cache(now)

    def get_fresh_price(self, asset: str, max_age: float = None) -> Optional[float]:
        now = time.time()
        limit = self.max_stale_seconds if max_age is None else max(0.0, float(max_age))
        try:
            updated = float(self.cache_updated_at.get(asset, 0.0))
        except (TypeError, ValueError, OverflowError):
            return None
        price = _positive_finite(self.cache.get(asset))
        if (price is None or not math.isfinite(updated) or updated <= 0
                or now - updated < 0 or now - updated > limit):
            return None
        return price

    def health(self) -> dict:
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
        if len(prices) <= bars:
            return 0.0
        current = _positive_finite(prices[-1])
        baseline = _positive_finite(prices[-1 - bars])
        if current is None or baseline is None:
            return 0.0
        result = (current - baseline) / baseline
        return result if math.isfinite(result) else 0.0

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
        # When klines fail (combined ~0), lean harder on local price history
        weight_local = 0.85 if abs(combined) < 0.02 else 0.30
        if lookback_prices and len(lookback_prices) >= 3:
            try:
                a, b = _positive_finite(lookback_prices[0]), _positive_finite(lookback_prices[-1])
                if a is not None and b is not None:
                    local_ret = (b - a) / a
                    local = max(-1.0, min(1.0, local_ret * 80))
                    combined = (1.0 - weight_local) * combined + weight_local * local
            except (TypeError, ValueError):
                pass
        return float(max(-1.0, min(1.0, combined)))
