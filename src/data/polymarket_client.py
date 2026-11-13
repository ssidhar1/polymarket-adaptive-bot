"""
Data layer for short-term crypto Up/Down markets on Polymarket.
Tries multiple slug patterns so a single rename does not zero markets.
"""
from typing import List, Dict, Optional, Any, Tuple
import json
import logging
import time
import threading
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

# Known Polymarket short-window slug templates (ts = window start unix)
SLUG_TEMPLATES = [
    "{asset}-updown-{label}-{ts}",
    "{asset}-up-or-down-{label}-{ts}",
    "{asset}-updown-{label}-{ts}z",
]


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
    s.headers.update({"User-Agent": "astf-bot/0.9"})
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
        self._lock = threading.Lock()
        self._working_template: Optional[str] = None
        logger.info("PolymarketData initialized (multi-slug discovery + CLOB)")

    def _get(self, url: str, params: dict = None, timeout: int = 8):
        with self._lock:
            return self.session.get(url, params=params or {}, timeout=timeout)

    def _market_by_slug(self, slug: str) -> Optional[Dict]:
        try:
            r = self._get(f"{GAMMA}/markets", params={"slug": slug}, timeout=8)
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
        templates = (
            [self._working_template] if self._working_template else list(SLUG_TEMPLATES)
        )
        for asset in assets:
            asset = asset.lower()
            if asset not in ASSETS:
                continue
            for label, step in WINDOWS:
                base = (now_i // step) * step
                for i in range(0, 2):
                    ts = base + i * step
                    for tmpl in templates:
                        if not tmpl:
                            continue
                        slug = tmpl.format(asset=asset, label=label, ts=ts)
                        out.append((asset, label, slug, step, ts))
        return out

    def _hydrate(self, meta: Tuple[str, str, str, int, int], now: float) -> Optional[Dict]:
        asset, label, slug, step, win_start = meta
        m = self._market_by_slug(slug)
        if not m:
            return None
        if m.get("closed") or not m.get("active", True):
            return None

        # Remember which template worked
        for tmpl in SLUG_TEMPLATES:
            try:
                if slug == tmpl.format(asset=asset, label=label, ts=win_start):
                    self._working_template = tmpl
                    break
            except Exception:
                pass

        end_iso = m.get("endDate")
        end_t = _end_ts(end_iso)
        if end_t == float("inf"):
            end_t = float(win_start + step)

        remaining = end_t - now
        if remaining < 45:
            return None
        if remaining > step + 30:
            return None
        if remaining > 45 * 60:
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
        m["_window_seconds"] = step
        m["_remaining_s"] = remaining
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

        with ThreadPoolExecutor(max_workers=8) as pool:
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
            # Reset sticky template so next cycle retries all patterns
            self._working_template = None
            logger.warning(
                "Zero markets — tried slug patterns %s; check Gamma / naming",
                SLUG_TEMPLATES,
            )
        return results

    def get_order_book(self, token_id: str) -> Dict:
        try:
            r = self._get(f"{CLOB}/book", params={"token_id": token_id}, timeout=8)
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
