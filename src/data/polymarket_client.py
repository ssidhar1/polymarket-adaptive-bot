"""
Data layer for short-term crypto Up/Down markets on Polymarket.
Live windows use deterministic slugs:
  {asset}-updown-{5m|15m}-{unix_aligned_to_window}
"""
from typing import List, Dict, Optional, Any, Tuple
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("astf.data")

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

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


def _build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "astf-bot/0.6"})
    retry = Retry(
        total=2,
        backoff_factor=0.3,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


class PolymarketData:
    def __init__(self):
        self.session = _build_session()
        logger.info("PolymarketData initialized (parallel slug discovery + CLOB)")

    def _market_by_slug(self, slug: str) -> Optional[Dict]:
        try:
            r = self.session.get(
                f"{GAMMA}/markets",
                params={"slug": slug},
                timeout=8,
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

    def _candidate_slugs(self, assets: List[str], now: float) -> List[Tuple[str, str, str, int, int]]:
        out = []
        now_i = int(now)
        for asset in assets:
            asset = asset.lower()
            if asset not in ASSETS:
                continue
            for label, step in WINDOWS:
                base = (now_i // step) * step
                # current + next few only (previous often already ending)
                for i in range(0, 4):
                    ts = base + i * step
                    slug = f"{asset}-updown-{label}-{ts}"
                    out.append((asset, label, slug, step, ts))
        return out

    def _hydrate(self, meta: Tuple[str, str, str, int, int], now: float) -> Optional[Dict]:
        asset, label, slug, step, win_start = meta
        m = self._market_by_slug(slug)
        if not m:
            return None
        if m.get("closed") or not m.get("active", True):
            return None

        end_iso = m.get("endDate")
        end_t = _end_ts(end_iso)
        if end_t == float("inf"):
            end_t = float(win_start + step)

        # Live / near-live only
        if end_t < now + 45:
            return None
        if end_t > now + 45 * 60:  # >45 min out — skip
            return None

        token_ids = m.get("clobTokenIds")
        if isinstance(token_ids, str):
            try:
                token_ids = json.loads(token_ids)
            except Exception:
                token_ids = []
        if not isinstance(token_ids, list) or len(token_ids) < 2:
            return None

        m["_up_token"] = str(token_ids[0])
        m["_down_token"] = str(token_ids[1])
        m["_asset"] = asset
        m["_series"] = f"{asset}-up-or-down-{label}"
        m["_end_ts"] = end_t
        m["_window"] = label
        if not m.get("slug"):
            m["slug"] = slug
        if not m.get("endDate"):
            m["endDate"] = datetime.fromtimestamp(end_t, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        return m

    def get_active_short_term_markets(self, assets: List[str] = None, limit: int = 100) -> List[Dict]:
        if assets is None:
            assets = ["btc", "eth"]

        now = time.time()
        candidates = self._candidate_slugs(assets, now)
        results: List[Dict] = []
        seen = set()

        # Parallel fetch — serial was 15–25s and starved the loop
        with ThreadPoolExecutor(max_workers=12) as pool:
            futs = {pool.submit(self._hydrate, c, now): c[2] for c in candidates}
            for fut in as_completed(futs):
                try:
                    m = fut.result()
                except Exception as e:
                    logger.debug(f"hydrate failed: {e}")
                    continue
                if not m:
                    continue
                slug = m.get("slug")
                if not slug or slug in seen:
                    continue
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
