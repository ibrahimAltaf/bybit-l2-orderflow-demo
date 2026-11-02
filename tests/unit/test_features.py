from __future__ import annotations

import math

import pytest

from bybit_demo.book.metrics import LiquidityFlowTracker, compute_book_metrics
from bybit_demo.book.reconstruction import OrderBook
from bybit_demo.features.absorption import measure_absorption
from bybit_demo.features.liquidity import SpreadRegime, book_pressure, spread_regime
from bybit_demo.features.volatility import VolatilityTracker
from bybit_demo.models.events import Side
from bybit_demo.models.orderbook import BandDepth, BookSide
from tests.helpers import D, delta, snap, trade

S = 1_000_000


def test_book_pressure_weighted_average() -> None:
    bands = (BandDepth(10, D(0), D(0), D(3), D(1)), BandDepth(25, D(0), D(0), D(1), D(3)))
    assert bands[0].imbalance == 0.5 and bands[1].imbalance == -0.5
    assert book_pressure(bands, (0.75, 0.25)) == pytest.approx(0.25)


def test_spread_regime() -> None:
    assert spread_regime(2.0, 3.0, 8.0) is SpreadRegime.TIGHT
    assert spread_regime(5.0, 3.0, 8.0) is SpreadRegime.NORMAL
    assert spread_regime(9.0, 3.0, 8.0) is SpreadRegime.WIDE


def test_absorption_requires_volume_and_small_displacement() -> None:
    absorbed = measure_absorption(
        D(3000), D(1000), 1.0, min_volume_vs_typical=1.5, max_displacement_bps=3.0
    )
    assert absorbed.absorbed and absorbed.volume_vs_typical == 3.0
    moved = measure_absorption(
        D(3000), D(1000), 10.0, min_volume_vs_typical=1.5, max_displacement_bps=3.0
    )
    assert not moved.absorbed
    small = measure_absorption(
        D(1000), D(1000), 0.0, min_volume_vs_typical=1.5, max_displacement_bps=3.0
    )
    assert not small.absorbed
    assert (
        measure_absorption(
            D(1), D(0), 0.0, min_volume_vs_typical=1.5, max_displacement_bps=3.0
        ).volume_vs_typical
        == 0.0
    )


def test_book_metrics_microprice_relative() -> None:
    b = OrderBook("T")
    b.apply_snapshot(snap(1, [("99", "3")], [("101", "1")]))
    m = compute_book_metrics(b, (100.0,))
    assert m is not None
    # More bid size -> microprice above mid: (3-1)/(2*(3+1)) = 0.25 of the spread
    assert m.microprice_rel == pytest.approx(0.25)
    assert m.spread_bps == pytest.approx(200.0)


def _liq_setup() -> tuple[OrderBook, LiquidityFlowTracker]:
    b = OrderBook("T")
    b.apply_snapshot(
        snap(1, [("99.99", "10"), ("99.98", "10")], [("100.01", "10"), ("100.02", "10")])
    )
    return b, LiquidityFlowTracker(band_bps=25.0, window_us=10 * S, replenish_memory_us=5 * S)


def test_level_decrease_attributed_to_execution_only_up_to_traded_qty() -> None:
    b, liq = _liq_setup()
    liq.on_trade(trade(1 * S, Side.BUY, "100.01", "3"))  # lifts the ask
    ch = b.apply_delta(delta(2, asks=[("100.01", "5")]))  # -5 at that level
    liq.on_book_changes(1 * S, ch, b.top().mid)  # type: ignore[union-attr]
    ask = liq.snapshot(1 * S)[BookSide.ASK]
    assert ask.executed == D(3) * D("100.01")
    assert ask.cancelled == D(2) * D("100.01")
    assert liq.snapshot(1 * S)[BookSide.BID].executed == 0


def test_replenishment_after_consumption() -> None:
    b, liq = _liq_setup()
    liq.on_trade(trade(1 * S, Side.SELL, "99.99", "4"))
    liq.on_book_changes(1 * S, b.apply_delta(delta(2, bids=[("99.99", "6")])), b.top().mid)  # type: ignore[union-attr]
    liq.on_book_changes(2 * S, b.apply_delta(delta(3, bids=[("99.99", "10")])), b.top().mid)  # type: ignore[union-attr]
    bid = liq.snapshot(2 * S)[BookSide.BID]
    assert bid.replenishment_ratio == pytest.approx(1.0)
    assert bid.net_withdrawal_ratio(D(1000)) < 0  # added > cancelled


def test_replenishment_outside_memory_not_counted() -> None:
    b, liq = _liq_setup()
    liq.on_trade(trade(1 * S, Side.SELL, "99.99", "4"))
    liq.on_book_changes(1 * S, b.apply_delta(delta(2, bids=[("99.99", "6")])), b.top().mid)  # type: ignore[union-attr]
    liq.on_book_changes(8 * S, b.apply_delta(delta(3, bids=[("99.99", "10")])), b.top().mid)  # type: ignore[union-attr]
    assert liq.snapshot(8 * S)[BookSide.BID].replenished == 0


def test_rpi_trade_never_attributed_to_visible_liquidity() -> None:
    b, liq = _liq_setup()
    liq.on_trade(trade(1 * S, Side.BUY, "100.01", "3", rpi=True))
    liq.on_book_changes(1 * S, b.apply_delta(delta(2, asks=[("100.01", "7")])), b.top().mid)  # type: ignore[union-attr]
    ask = liq.snapshot(1 * S)[BookSide.ASK]
    assert ask.executed == 0 and ask.cancelled == D(3) * D("100.01")


def test_liquidity_outside_band_ignored_and_window_expires() -> None:
    b, liq = _liq_setup()
    liq.on_book_changes(1 * S, b.apply_delta(delta(2, asks=[("105", "100")])), b.top().mid)  # type: ignore[union-attr]
    assert liq.snapshot(1 * S)[BookSide.ASK].added == 0
    liq.on_book_changes(2 * S, b.apply_delta(delta(3, asks=[("100.02", "0")])), b.top().mid)  # type: ignore[union-attr]
    assert liq.snapshot(2 * S)[BookSide.ASK].cancelled > 0
    assert liq.snapshot(13 * S)[BookSide.ASK].cancelled == 0


def test_volatility_compression_and_extension() -> None:
    v = VolatilityTracker(short_s=10, long_s=100, extension_window_s=50, min_sigma_bps=1.0)
    mid = D(100)
    # 10 one-bps oscillations early in the long window (ending at 100), then calm.
    for i in range(11):
        v.on_mid(i * S, mid + D("0.01") * (i % 2))
    snap_calm = v.snapshot(60 * S, mid)
    assert snap_calm.compression_ratio == 0.0
    r2 = math.log(100.01 / 100) ** 2
    assert snap_calm.rv_long_bps == pytest.approx(math.sqrt(10 * r2) * 1e4, rel=1e-6)
    v.on_mid(61 * S, D("100.10"))  # +10 bps jump
    s = v.snapshot(61 * S, D("100.10"))
    assert s.compression_ratio is not None and s.compression_ratio > 1.0
    assert s.ext_up_bps == pytest.approx(10.0, rel=1e-3)
    assert s.ext_up_sigma == pytest.approx(s.ext_up_bps / s.sigma_ext_bps)
    assert s.ext_down_bps == 0.0


def test_extension_counts_value_set_before_window_start() -> None:
    """Regression: a mid set before the window but still in force is part of it."""
    v = VolatilityTracker(10, 100, extension_window_s=50, min_sigma_bps=1.0)
    v.on_mid(0, D(100))
    v.on_mid(80 * S, D("100.20"))  # last change 80 s after the previous one
    assert v.snapshot(80 * S, D("100.20")).ext_up_bps == pytest.approx(20.0, rel=1e-3)
    # Once the old value is fully outside the window, the extension resets.
    assert v.snapshot(131 * S, D("100.20")).ext_up_bps == 0.0


def test_volatility_insufficient_history() -> None:
    v = VolatilityTracker(10, 100, 50, 3.0)
    v.on_mid(0, D(100))
    s = v.snapshot(1, D(100))
    assert s.compression_ratio is None
    assert s.sigma_ext_bps == 3.0
