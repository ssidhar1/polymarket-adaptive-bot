"""Weekly crypto price/threshold market discovery (observation-only).

This scanner deliberately does not estimate fair probability. It discovers
eligible binary markets, maps the YES token by outcome label, snapshots an
executable CLOB ask/depth, and emits an Opportunity with fair_probability=None.
Until a separately validated probability model and resolution-rule audit exist,
these candidates cannot pass the evidence gate or become paper orders.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import requests

from src.strategies.base import Opportunity
from src.utils.fees import taker_fee_rate


_CRYPTO_TERMS = re.compile(r"\b(bitcoin|btc|ethereum|ether|eth)\b", re.IGNORECASE)
_PRICE_TERMS = re.compile(
    r"\b(price|above|below|between|reach|close|range|end the week|weekly)\b",
    re.IGNORECASE,
)


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _parse_array(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (TypeError, ValueError):
            return []
    return []


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _is_weekly_crypto_market(market: dict, now: datetime, horizon: datetime) -> bool:
    question = str(market.get("question") or "")
    if not _CRYPTO_TERMS.search(question) or not _PRICE_TERMS.search(question):
        return False
    end_at = _parse_datetime(market.get("endDate") or market.get("end_date"))
    if end_at is None or not now < end_at <= horizon:
        return False
    if market.get("closed") is True or market.get("active") is False:
        return False
    return True


def _yes_token(market: dict) -> str | None:
    outcomes = [str(x).strip().casefold() for x in _parse_array(market.get("outcomes"))]
    token_ids = [str(x).strip() for x in _parse_array(
        market.get("clobTokenIds") or market.get("positionIds")
    )]
    if len(outcomes) != len(token_ids) or len(outcomes) < 2:
        return None
    matches = [i for i, outcome in enumerate(outcomes) if outcome == "yes"]
    if len(matches) != 1 or not token_ids[matches[0]]:
        return None
    return token_ids[matches[0]]


def _book_snapshot(book: dict) -> tuple[Decimal | None, Decimal]:
    parsed = []
    for level in book.get("asks") or []:
        if isinstance(level, dict):
            price, size = _decimal(level.get("price")), _decimal(level.get("size"))
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            price, size = _decimal(level[0]), _decimal(level[1])
        else:
            continue
        if price is not None and size is not None and Decimal("0") < price < Decimal("1") and size > 0:
            parsed.append((price, size))
    parsed.sort(key=lambda item: item[0])
    if not parsed:
        return None, Decimal("0")
    depth_usd = sum((price * size for price, size in parsed[:5]), Decimal("0"))
    return parsed[0][0], depth_usd


class WeeklyPriceRangeScanner:
    """Paginated Gamma + CLOB scanner. It never places orders."""

    name = "weekly_price_ranges"

    def __init__(
        self,
        *,
        page_size: int = 100,
        max_pages: int = 10,
        min_depth_usd: Decimal | str = Decimal("10"),
        default_fee_rate: Decimal | str = Decimal("0.07"),
        slippage_bps: Decimal | str = Decimal("20"),
        session: Any = None,
        clock=None,
    ) -> None:
        self.page_size = max(1, min(500, int(page_size)))
        self.max_pages = max(1, int(max_pages))
        self.min_depth_usd = Decimal(str(min_depth_usd))
        self.default_fee_rate = Decimal(str(default_fee_rate))
        self.slippage_bps = Decimal(str(slippage_bps))
        if self.min_depth_usd < 0 or self.default_fee_rate < 0 or self.slippage_bps < 0:
            raise ValueError("depth, fee rate and slippage assumptions must be non-negative")
        self.session = session or requests.Session()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.gamma_base = "https://gamma-api.polymarket.com"
        self.clob_base = "https://clob.polymarket.com"

    async def scan(self) -> list[Opportunity]:
        return await asyncio.to_thread(self._scan_sync)

    def close(self) -> None:
        close = getattr(self.session, "close", None)
        if callable(close):
            close()

    def _get_json(self, url: str, params: dict | None = None) -> Any:
        response = self.session.get(url, params=params or {}, timeout=12)
        response.raise_for_status()
        return response.json()

    def _list_markets(self, now: datetime, horizon: datetime) -> list[dict]:
        markets: list[dict] = []
        seen: set[str] = set()
        for page in range(self.max_pages):
            batch = self._get_json(
                f"{self.gamma_base}/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": self.page_size,
                    "offset": page * self.page_size,
                    "order": "endDate",
                    "ascending": "true",
                    "end_date_min": now.isoformat(),
                    "end_date_max": horizon.isoformat(),
                },
            )
            if not isinstance(batch, list) or not batch:
                break
            for market in batch:
                if not isinstance(market, dict):
                    continue
                identity = str(market.get("conditionId") or market.get("id") or market.get("slug") or "")
                if identity and identity not in seen:
                    seen.add(identity)
                    markets.append(market)
            if len(batch) < self.page_size:
                break
        return markets

    def _scan_sync(self) -> list[Opportunity]:
        now = self.clock().astimezone(timezone.utc)
        horizon = now + timedelta(days=7)
        opportunities: list[Opportunity] = []
        for market in self._list_markets(now, horizon):
            if not _is_weekly_crypto_market(market, now, horizon):
                continue
            token_id = _yes_token(market)
            market_id = str(market.get("conditionId") or market.get("id") or "")
            if not token_id or not market_id:
                continue
            try:
                book = self._get_json(f"{self.clob_base}/book", {"token_id": token_id})
            except requests.RequestException:
                continue
            if not isinstance(book, dict):
                continue
            ask, depth_usd = _book_snapshot(book)
            if ask is None or depth_usd < self.min_depth_usd:
                continue

            fee_schedule = market.get("feeSchedule")
            if isinstance(fee_schedule, str):
                try:
                    fee_schedule = json.loads(fee_schedule)
                except ValueError:
                    fee_schedule = {}
            fee_rate = _decimal((fee_schedule or {}).get("rate")) if isinstance(fee_schedule, dict) else None
            flags = ["fair_value_unmodeled", "resolution_rules_unverified"]
            if fee_rate is None:
                fee_rate = self.default_fee_rate
                flags.append("fee_schedule_defaulted")
            fee_fraction = Decimal(str(taker_fee_rate(float(ask), rate=float(fee_rate))))
            fee_per_share = ask * fee_fraction
            slippage_per_share = ask * self.slippage_bps / Decimal("10000")
            end_at = _parse_datetime(market.get("endDate") or market.get("end_date"))
            question = str(market.get("question") or "")
            opportunities.append(Opportunity(
                strategy=self.name,
                market_id=market_id,
                token_id=token_id,
                side="yes",
                observed_at=now,
                executable_price=ask,
                fair_probability=None,
                estimated_fees=fee_per_share,
                estimated_slippage=slippage_per_share,
                available_depth=depth_usd,
                resolution_at=end_at,
                evidence=("gamma_market_metadata", "clob_order_book"),
                risk_flags=tuple(flags),
                metadata={
                    "question": question,
                    "slug": market.get("slug"),
                    "outcomes": _parse_array(market.get("outcomes")),
                    "fee_rate": str(fee_rate),
                    "fee_source": "gamma_fee_schedule" if "fee_schedule_defaulted" not in flags else "conservative_config_fallback",
                    "book_depth_usd_top5": str(depth_usd),
                    "source": "gamma_api+clob_api",
                    "observation_only": True,
                },
            ))
        return opportunities
