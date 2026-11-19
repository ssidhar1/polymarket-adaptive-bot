import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.strategies.weekly_price_ranges import (
    WeeklyPriceRangeScanner,
    _book_snapshot,
    _is_weekly_crypto_market,
    _yes_token,
)


def test_yes_token_is_mapped_by_outcome_not_assumed_index_zero():
    market = {
        "outcomes": '["No", "Yes"]',
        "clobTokenIds": '["token-no", "token-yes"]',
    }
    assert _yes_token(market) == "token-yes"


def test_yes_token_rejects_misaligned_or_ambiguous_metadata():
    assert _yes_token({"outcomes": '["Yes", "No"]', "clobTokenIds": '["only-one"]'}) is None
    assert _yes_token({"outcomes": '["Yes", "YES"]', "clobTokenIds": '["a", "b"]'}) is None


def test_weekly_crypto_filter_requires_crypto_price_and_future_end_date():
    now = datetime(2026, 11, 19, tzinfo=timezone.utc)
    market = {
        "question": "Will Bitcoin price be above $100,000 at the end of the week?",
        "endDate": (now + timedelta(days=3)).isoformat(),
        "active": True,
        "closed": False,
    }
    assert _is_weekly_crypto_market(market, now, now + timedelta(days=7))
    market["endDate"] = (now + timedelta(days=8)).isoformat()
    assert not _is_weekly_crypto_market(market, now, now + timedelta(days=7))


def test_book_snapshot_sorts_asks_and_computes_dollar_depth():
    ask, depth = _book_snapshot({
        "asks": [
            {"price": "0.70", "size": "20"},
            {"price": "0.60", "size": "10"},
            {"price": "0.65", "size": "10"},
        ]
    })
    assert ask == Decimal("0.60")
    assert depth == Decimal("26.5")


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class FakeSession:
    def __init__(self, market, book):
        self.market = market
        self.book = book
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        if url.endswith("/markets"):
            return FakeResponse([self.market])
        return FakeResponse(self.book)


def test_scanner_emits_observation_only_candidate_without_inventing_fair_value():
    now = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
    market = {
        "id": "market-1",
        "conditionId": "condition-1",
        "question": "Will BTC price be above $100k by Friday?",
        "slug": "btc-above-100k-week",
        "endDate": (now + timedelta(days=2)).isoformat(),
        "active": True,
        "closed": False,
        "outcomes": '["No", "Yes"]',
        "clobTokenIds": '["no-token", "yes-token"]',
        "feeSchedule": {"rate": 0.07},
        "description": "Resolves from the stated market source.",
    }
    session = FakeSession(market, {
        "asks": [{"price": "0.80", "size": "100"}],
        "bids": [{"price": "0.79", "size": "100"}],
    })
    scanner = WeeklyPriceRangeScanner(
        session=session,
        clock=lambda: now,
        min_depth_usd="10",
        slippage_bps="20",
    )
    items = asyncio.run(scanner.scan())
    assert len(items) == 1
    item = items[0]
    assert item.token_id == "yes-token"
    assert item.fair_probability is None
    assert item.expected_net_edge_per_share is None
    assert "fair_value_unmodeled" in item.risk_flags
    assert "resolution_rules_unverified" in item.risk_flags
    assert item.estimated_fees > 0
    assert item.estimated_slippage > 0
    assert item.metadata["observation_only"] is True
    assert session.calls[1][1] == {"token_id": "yes-token"}


def test_scanner_does_not_emit_when_book_depth_is_too_thin():
    now = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
    market = {
        "id": "market-1",
        "conditionId": "condition-1",
        "question": "Will Ethereum price be above $4,000 this week?",
        "endDate": (now + timedelta(days=1)).isoformat(),
        "active": True,
        "closed": False,
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["yes-token", "no-token"]',
    }
    session = FakeSession(market, {"asks": [{"price": "0.5", "size": "1"}]})
    scanner = WeeklyPriceRangeScanner(session=session, clock=lambda: now, min_depth_usd="10")
    assert asyncio.run(scanner.scan()) == []
