"""
Robust market resolution checker.
Only treats a market as settled when outcome prices are decisive (~0/1),
so we don't lock PnL on intermediate 0.5/0.5 closed flags.
"""
from typing import Optional, Tuple, Any, Dict
import logging
import json
import time
import requests

logger = logging.getLogger("astf.resolution")
GAMMA = "https://gamma-api.polymarket.com"


def _parse_labeled_outcome_prices(outcomes_raw: Any, prices_raw: Any) -> Tuple[Optional[float], Optional[float]]:
    """Map final prices by outcome label, never by array position."""
    def _array(raw):
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                return None
        return raw if isinstance(raw, list) else None

    outcomes, prices = _array(outcomes_raw), _array(prices_raw)
    if outcomes is None or prices is None or len(outcomes) != len(prices):
        return None, None
    up_idx = [i for i, x in enumerate(outcomes) if str(x).strip().casefold() in {"up", "yes"}]
    down_idx = [i for i, x in enumerate(outcomes) if str(x).strip().casefold() in {"down", "no"}]
    if len(up_idx) != 1 or len(down_idx) != 1 or up_idx[0] == down_idx[0]:
        return None, None
    try:
        up_p, down_p = float(prices[up_idx[0]]), float(prices[down_idx[0]])
    except (TypeError, ValueError, IndexError):
        return None, None
    if not (0 <= up_p <= 1 and 0 <= down_p <= 1):
        return None, None
    return up_p, down_p


def _parse_labeled_tokens(tokens: Any) -> Tuple[Optional[float], Optional[float]]:
    if not isinstance(tokens, list) or len(tokens) < 2:
        return None, None
    mapped = {}
    for token in tokens:
        if not isinstance(token, dict):
            continue
        label = str(token.get("outcome") or token.get("name") or "").strip().casefold()
        if label in {"up", "yes"}:
            key = "up"
        elif label in {"down", "no"}:
            key = "down"
        else:
            continue
        if key in mapped:
            return None, None
        try:
            if token.get("winner") is True:
                mapped[key] = 1.0
            elif token.get("winner") is False and token.get("price") is None:
                mapped[key] = 0.0
            elif token.get("price") is not None:
                mapped[key] = float(token["price"])
        except (TypeError, ValueError):
            return None, None
    if set(mapped) != {"up", "down"}:
        return None, None
    up_p, down_p = mapped["up"], mapped["down"]
    if not (0 <= up_p <= 1 and 0 <= down_p <= 1):
        return None, None
    return up_p, down_p


def _decisive(up_p: Optional[float], down_p: Optional[float]) -> bool:
    if up_p is None or down_p is None:
        return False
    return (up_p >= 0.95 and down_p <= 0.05) or (down_p >= 0.95 and up_p <= 0.05)


def _extract_from_market(m: Dict) -> Tuple[bool, Optional[float], Optional[float]]:
    flagged_closed = bool(m.get("closed"))
    if m.get("umaResolutionStatus") in ("resolved", "resolved_yes", "resolved_no"):
        flagged_closed = True

    up_p, down_p = _parse_labeled_outcome_prices(
        m.get("outcomes"), m.get("outcomePrices")
    )
    if up_p is None or down_p is None:
        up_p, down_p = _parse_labeled_tokens(m.get("tokens"))

    # Only report closed=True when we can settle cleanly
    if _decisive(up_p, down_p):
        return True, up_p, down_p

    if flagged_closed and up_p is not None and down_p is not None:
        # Closed but not decisive yet — do not settle (avoid locking 0.5 PnL)
        return False, up_p, down_p

    return False, up_p, down_p


class ResolutionChecker:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "astf-bot/0.6"})
        self._cache: Dict[str, Tuple[float, Tuple]] = {}
        self._cache_ttl = 12.0

    def _get_json(self, url: str, params: dict = None) -> Any:
        r = self.session.get(url, params=params or {}, timeout=10)
        r.raise_for_status()
        return r.json()

    def _lookup_market(self, market_id: str = None, slug: str = None) -> Optional[Dict]:
        attempts = []
        if slug:
            attempts.append(("markets_query", f"{GAMMA}/markets", {"slug": slug}))
            attempts.append(("markets_path", f"{GAMMA}/markets/slug/{slug}", None))
            attempts.append(("events_query", f"{GAMMA}/events", {"slug": slug}))
        if market_id and not str(market_id).startswith("btc-") and not str(market_id).startswith("eth-"):
            attempts.append(("markets_id", f"{GAMMA}/markets/{market_id}", None))

        for name, url, params in attempts:
            try:
                data = self._get_json(url, params)
                if isinstance(data, list):
                    if not data:
                        continue
                    item = data[0]
                else:
                    item = data

                if isinstance(item, dict) and "markets" in item and isinstance(item["markets"], list):
                    if item["markets"]:
                        m = item["markets"][0]
                        if item.get("closed") and not m.get("closed"):
                            m = dict(m)
                            m["closed"] = True
                        return m
                if isinstance(item, dict) and (
                    item.get("conditionId") or item.get("clobTokenIds") or item.get("question")
                ):
                    return item
            except Exception as e:
                logger.debug(f"Resolution path {name} failed: {e}")
                continue
        return None

    def check(self, market_id: str = None, slug: str = None) -> Tuple[bool, Optional[float], Optional[float]]:
        key = f"{slug or ''}|{market_id or ''}"
        now = time.time()
        if key in self._cache:
            ts, val = self._cache[key]
            if now - ts < self._cache_ttl:
                return val

        if not slug and not market_id:
            return False, None, None

        m = self._lookup_market(market_id=market_id, slug=slug)
        if not m:
            result = (False, None, None)
            self._cache[key] = (now, result)
            return result

        result = _extract_from_market(m)
        # Short cache when not decisive so we re-check soon
        ttl_now = now if result[0] else now - (self._cache_ttl - 5)
        self._cache[key] = (ttl_now, result)
        return result

    def token_final_price(
        self,
        direction: str,
        up_price: Optional[float],
        down_price: Optional[float],
    ) -> Optional[float]:
        if up_price is None or down_price is None:
            return None
        if not _decisive(up_price, down_price):
            return None
        # Snap to binary for clean PnL
        if direction == "up":
            return 1.0 if up_price >= 0.95 else 0.0
        return 1.0 if down_price >= 0.95 else 0.0
