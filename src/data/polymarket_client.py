"""
Data layer for short-term crypto Up/Down markets on Polymarket.
Tries multiple slug patterns so a single rename does not zero markets.
"""
from typing import List, Dict, Optional, Any, Tuple
import json
import logging
import math
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


def _outcome_token_ids(market: Dict) -> Tuple[Optional[str], Optional[str]]:
    """Map UP/DOWN token IDs from outcome labels; never trust array position."""
    outcomes = market.get("outcomes")
    token_ids = market.get("clobTokenIds")
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except (TypeError, ValueError):
            outcomes = None
    if isinstance(token_ids, str):
        try:
            token_ids = json.loads(token_ids)
        except (TypeError, ValueError):
            token_ids = None
    if not isinstance(outcomes, list) or not isinstance(token_ids, list):
        return None, None
    if len(outcomes) != len(token_ids) or len(outcomes) < 2:
        return None, None

    up_aliases = {"up", "yes"}
    down_aliases = {"down", "no"}
    up = [i for i, label in enumerate(outcomes) if str(label).strip().casefold() in up_aliases]
    down = [i for i, label in enumerate(outcomes) if str(label).strip().casefold() in down_aliases]
    if len(up) != 1 or len(down) != 1 or up[0] == down[0]:
        return None, None
    up_id, down_id = str(token_ids[up[0]]).strip(), str(token_ids[down[0]]).strip()
    if not up_id or not down_id or up_id == down_id:
        return None, None
    return up_id, down_id


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
        # A shared lock around session.get serialized every request, making
        # ThreadPoolExecutor discovery effectively sequential. Give each
        # worker its own pooled Session so independent Gamma requests overlap.
        self._local = threading.local()
        self._working_template: Optional[str] = None
        self._fee_cache = {}
        self._fee_cache_lock = threading.Lock()
        logger.info("PolymarketData initialized (multi-slug discovery + CLOB)")

    def _get(self, url: str, params: dict = None, timeout: int = 8):
        session = getattr(self._local, "session", None)
        if session is None:
            session = _build_session()
            self._local.session = session
        return session.get(url, params=params or {}, timeout=timeout)

    def get_fee_schedule(self, token_id: str, condition_id: str = None,
                         default_rate: float = 0.07) -> Dict[str, Any]:
        """Resolve the public CLOB fee coefficient for this market/token.

        Prefer CLOB market metadata (fd.r); fall back to the token fee-rate
        endpoint (base_fee is basis points). Never interpret a valid zero rate
        as missing. Cache both verified and fallback values briefly.
        """
        token = str(token_id or "").strip()
        condition = str(condition_id or "").strip()
        key = (condition, token)
        now = time.monotonic()
        with self._fee_cache_lock:
            cached = self._fee_cache.get(key)
            if cached and cached["expires_at"] > now:
                return dict(cached["value"])

        value = None
        if condition:
            try:
                response = self._get(f"{CLOB}/markets/{condition}", timeout=6)
                response.raise_for_status()
                payload = response.json() or {}
                fd = payload.get("fd") or payload.get("feeSchedule") or {}
                if isinstance(fd, dict):
                    raw_rate = fd.get("r", fd.get("rate"))
                    if raw_rate is not None:
                        rate = float(raw_rate)
                        exponent = float(fd.get("e", fd.get("exponent", 1)) or 1)
                        if (math.isfinite(rate) and math.isfinite(exponent)
                                and 0 <= rate <= 0.25 and 0 < exponent <= 4):
                            value = {
                                "rate": rate, "exponent": exponent,
                                "source": "clob_market_info", "verified": True,
                            }
            except Exception as exc:
                logger.debug("CLOB market fee metadata unavailable for %s: %s", condition, exc)

        if value is None and token:
            try:
                response = self._get(f"{CLOB}/fee-rate", params={"token_id": token}, timeout=6)
                response.raise_for_status()
                payload = response.json() or {}
                if "base_fee" in payload and payload["base_fee"] is not None:
                    rate = float(payload["base_fee"]) / 10_000.0
                    if math.isfinite(rate) and 0 <= rate <= 0.25:
                        value = {
                            "rate": rate, "exponent": 1.0,
                            "source": "clob_fee_rate_bps", "verified": True,
                        }
            except Exception as exc:
                logger.debug("CLOB token fee-rate unavailable for %s: %s", token, exc)

        if value is None:
            value = {
                "rate": float(default_rate), "exponent": 1.0, "collection": "usdc",
                "source": "conservative_config_fallback", "verified": False,
            }
        ttl = 300 if value["verified"] else 30
        with self._fee_cache_lock:
            self._fee_cache[key] = {"expires_at": now + ttl, "value": dict(value)}
        return value

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

        up_token, down_token = _outcome_token_ids(m)
        if not up_token or not down_token:
            logger.warning(
                "Skipping market with ambiguous outcome/token mapping: %s",
                m.get("slug") or m.get("id") or "unknown",
            )
            return None

        fee_info = (
            {"rate": 0.0, "exponent": 1.0, "collection": "usdc", "source": "gamma_fees_disabled", "verified": True}
            if m.get("feesEnabled") is False
            else self.get_fee_schedule(
                up_token, m.get("conditionId") or m.get("condition_id"), default_rate=0.07
            )
        )
        m["_fee_rate"] = fee_info["rate"]
        m["_fee_exponent"] = fee_info["exponent"]
        m["_fee_collection"] = fee_info.get("collection", "usdc")
        m["_fee_source"] = fee_info["source"]
        m["_fee_verified"] = fee_info["verified"]
        m["_up_token"] = up_token
        m["_down_token"] = down_token
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
                "fetched_at": time.time(),
                "raw": data,
            }
        except Exception as e:
            tid = (token_id or "")[:12]
            logger.warning(f"Book fetch failed for {tid}...: {e}")
            return {"bids": [], "asks": [], "mid": None, "best_bid": None, "best_ask": None, "fetched_at": time.time()}

    def get_mid_price(self, token_id: str) -> Optional[float]:
        return self.get_order_book(token_id).get("mid")
