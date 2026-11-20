import pytest

from src.execution.paper import PaperExecutor
from src.utils.fees import taker_fee_rate


def test_aggressive_fill_pays_taker_fee_even_when_maker_preferred():
    executor = PaperExecutor(fee_rate=0.07, prefer_maker=True, slippage_bps=0)
    order = executor.place_order("token", "buy", 0.50, 10.0, "market", already_aggressive=True)

    assert order is not None
    assert order["maker"] is False
    assert order["execution_type"] == "taker"
    assert order["fee_frac"] == pytest.approx(taker_fee_rate(0.50, 0.07))


def test_buy_fill_applies_adverse_slippage():
    executor = PaperExecutor(fee_rate=0.07, prefer_maker=True, slippage_bps=20)
    order = executor.place_order("token", "buy", 0.50, 10.0, "market")

    assert order is not None
    assert order["fill_price"] == pytest.approx(0.501)
    assert order["slippage_bps"] == pytest.approx(20)
    assert order["fill_price"] > order["requested_price"]


def test_sell_fill_applies_adverse_slippage():
    executor = PaperExecutor(fee_rate=0.07, prefer_maker=True, slippage_bps=20)
    order = executor.place_order("token", "sell", 0.50, 10.0, "market")

    assert order is not None
    assert order["fill_price"] == pytest.approx(0.499)
    assert order["fill_price"] < order["requested_price"]


def test_fill_rejected_if_slippage_exits_binary_price_domain():
    executor = PaperExecutor(fee_rate=0.07, slippage_bps=500)
    assert executor.place_order("token", "buy", 0.99, 10.0, "market") is None


def test_market_specific_fee_rate_overrides_default():
    executor = PaperExecutor(fee_rate=0.07, slippage_bps=0)
    order = executor.place_order("token", "buy", 0.50, 10.0, "market", fee_rate=0.03)
    assert order is not None
    assert order["fee_rate"] == pytest.approx(0.03)
    assert order["fee_frac"] == pytest.approx(taker_fee_rate(0.50, 0.03))


def test_buy_consumes_real_ask_ladder_and_reports_vwap():
    executor = PaperExecutor(fee_rate=0.07, slippage_bps=0, max_slippage_bps=100)
    order = executor.place_order(
        "token", "buy", 0.50, 8.0, "market",
        book_asks=[
            {"price": "0.50", "size": "10"},
            {"price": "0.51", "size": "10"},
        ],
    )
    assert order is not None
    expected_shares = 10.0 + (3.0 / 0.51)
    assert order["shares"] == pytest.approx(expected_shares)
    assert order["fill_price"] == pytest.approx(8.0 / expected_shares)
    assert order["fill_price"] > 0.50
    assert order["slippage_bps"] > 0


def test_buy_rejects_when_displayed_ask_depth_cannot_fill_size():
    executor = PaperExecutor(fee_rate=0.07, slippage_bps=0, max_slippage_bps=100)
    order = executor.place_order(
        "token", "buy", 0.50, 10.0, "market",
        book_asks=[{"price": "0.50", "size": "2"}],
    )
    assert order is None


def test_buy_rejects_when_book_walk_exceeds_slippage_budget():
    executor = PaperExecutor(fee_rate=0.07, slippage_bps=0, max_slippage_bps=20)
    order = executor.place_order(
        "token", "buy", 0.50, 8.0, "market",
        book_asks=[
            {"price": "0.50", "size": "10"},
            {"price": "0.60", "size": "10"},
        ],
    )
    assert order is None


def test_buy_rejects_ask_ladder_if_best_ask_does_not_match_requested_price():
    executor = PaperExecutor(fee_rate=0.07, slippage_bps=0)
    order = executor.place_order(
        "token", "buy", 0.49, 5.0, "market",
        book_asks=[{"price": "0.50", "size": "20"}],
    )
    assert order is None
