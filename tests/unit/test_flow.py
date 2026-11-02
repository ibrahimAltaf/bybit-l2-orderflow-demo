from __future__ import annotations

from decimal import Decimal

import pytest

from bybit_demo.errors import ReplayOrderError, SchemaError
from bybit_demo.flow.aggressor import TickRuleClassifier, parse_reported_side
from bybit_demo.flow.cvd import CVDTracker, EventFlowWindow, FlowWindow
from bybit_demo.flow.rolling import (
    CountWindowSum,
    FloatTimeWindowSum,
    LaggedValue,
    TimeWindowExtrema,
    TimeWindowSum,
)
from bybit_demo.models.events import Side
from tests.helpers import D, trade

S = 1_000_000


def test_signed_trade_quantities() -> None:
    assert trade(1, Side.BUY, "10", "2").signed_quantity == D(2)
    assert trade(1, Side.SELL, "10", "2").signed_quantity == D(-2)
    assert trade(1, Side.BUY, "10.5", "2").notional == D("21.0")


def test_cvd_cumulative_exact() -> None:
    c = CVDTracker()
    for t in (
        trade(1, Side.BUY, "10", "3"),
        trade(2, Side.SELL, "11", "1"),
        trade(3, Side.BUY, "0.1", "0.3"),
    ):
        c.on_trade(t)
    assert c.cvd_qty == D("2.3")
    assert c.cvd_notional == D("30") - D("11") + D("0.03")
    assert c.buy_notional == D("30.03") and c.sell_notional == D("11")
    assert (c.buy_count, c.sell_count, c.inferred_count) == (2, 1, 0)


def test_time_window_evicts_strictly_older_than_window() -> None:
    w = TimeWindowSum(5 * S)
    w.add(0, D(1))
    w.add(2 * S, D(2))
    w.advance(5 * S - 1)
    assert w.total == D(3)
    w.advance(5 * S)  # t=0 now exactly window old -> out
    assert w.total == D(2)
    w.advance(7 * S)
    assert w.total == D(0) and w.count == 0


def test_window_refuses_time_travel() -> None:
    w = TimeWindowSum(S)
    w.add(10, D(1))
    with pytest.raises(ReplayOrderError):
        w.add(5, D(1))
    f = FloatTimeWindowSum(S)
    f.add(10, 1.0)
    with pytest.raises(ReplayOrderError):
        f.add(5, 1.0)
    e = TimeWindowExtrema(S)
    e.add(10, D(1))
    with pytest.raises(ReplayOrderError):
        e.add(5, D(1))


def test_no_future_leakage_value_read_before_push() -> None:
    """A read at time t is unaffected by values pushed afterwards."""
    w = FlowWindow(10 * S)
    w.on_trade(trade(1 * S, Side.BUY, "10", "1"))
    before = (w.signed_notional, w.imbalance)
    w.on_trade(trade(2 * S, Side.SELL, "10", "5"))
    assert before == (D(10), 1.0)
    assert w.signed_notional == D(-40)


def test_flow_window_metrics() -> None:
    w = FlowWindow(5 * S)
    w.on_trade(trade(0, Side.BUY, "10", "3"))
    w.on_trade(trade(1 * S, Side.SELL, "10", "1"))
    w.on_trade(trade(2 * S, Side.BUY, "10", "1"))
    assert w.signed_notional == D(30)
    assert w.imbalance == pytest.approx(30 / 50)
    assert w.count_imbalance == pytest.approx(1 / 3)
    assert w.trade_count == 3
    assert w.largest_trade() == D(30)
    w.advance(5 * S + 1)  # first trade evicted
    assert w.signed_notional == D(0) and w.trade_count == 2


def test_event_count_window() -> None:
    e = EventFlowWindow(2)
    e.on_trade(trade(0, Side.BUY, "1", "10"))
    e.on_trade(trade(1, Side.SELL, "1", "1"))
    e.on_trade(trade(2, Side.SELL, "1", "1"))
    assert e.imbalance == pytest.approx(-1.0)
    c = CountWindowSum(3)
    for v in (1, 2, 3, 4):
        c.add(D(v))
    assert c.total == D(9) and c.full


def test_multiple_rolling_windows_are_independent() -> None:
    short, long = FlowWindow(1 * S), FlowWindow(10 * S)
    for ts, side in ((0, Side.SELL), (5 * S, Side.BUY)):
        t = trade(ts, side, "1", "1")
        short.on_trade(t)
        long.on_trade(t)
    assert short.imbalance == 1.0
    assert long.imbalance == 0.0


def test_extrema_and_lagged_value() -> None:
    e = TimeWindowExtrema(10)
    for t, v in ((0, 5), (2, 3), (4, 8), (6, 4)):
        e.add(t, D(v))
    assert (e.minimum, e.maximum) == (D(3), D(8))
    e.advance(13)  # (2,3) evicted (2 <= 13-10)
    assert e.minimum == D(4)
    lag = LaggedValue(10)
    lag.add(0, D(1))
    lag.add(5, D(2))
    lag.add(12, D(3))
    assert lag.value_at_lag(9) is None
    assert lag.value_at_lag(15) == D(2)  # latest sample at or before t=5
    assert lag.value_at_lag(22) == D(3)


def test_float_window_resummation_is_stable() -> None:
    w = FloatTimeWindowSum(10)
    for i in range(5000):
        w.add(i, 0.1)
    assert w.total == pytest.approx(1.0, abs=1e-9)


def test_reported_side_parsing() -> None:
    assert parse_reported_side("Buy") is Side.BUY
    assert parse_reported_side("sell") is Side.SELL
    with pytest.raises(SchemaError):
        parse_reported_side("unknown")


def test_tick_rule_inference_is_explicit_and_causal() -> None:
    tr = TickRuleClassifier()
    prices = [D("10"), D("10.1"), D("10.1"), D("10.0"), D("10.0")]
    assert [tr.classify(p) for p in prices] == [None, Side.BUY, Side.BUY, Side.SELL, Side.SELL]


def test_decimal_cvd_has_no_float_drift() -> None:
    c = CVDTracker()
    for i in range(1000):
        c.on_trade(trade(i, Side.BUY if i % 2 else Side.SELL, "0.1", "0.1"))
    assert c.cvd_notional == Decimal(0)
