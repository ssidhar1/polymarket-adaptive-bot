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
