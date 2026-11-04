"""Causality of the availability clocks. Timestamps here are invented small integers."""

from __future__ import annotations

import gzip
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from bybit_demo.audit.logger import MemorySink
from bybit_demo.book.continuity import BookSynchronizer, BybitContinuityValidator
from bybit_demo.book.reconstruction import OrderBook
from bybit_demo.detector.engine import DetectorEngine
from bybit_demo.errors import ConfigError, MissingTimestampError, NegativeLatencyError
from bybit_demo.features.feature_engine import FeatureEngine
from bybit_demo.models.events import BookDelta, BookEvent, MarketEvent, Side, TradeEvent
from bybit_demo.replay.availability import (
    ClockBasis,
    ClockMode,
    ClockScenario,
    ClockSetup,
    measure_collector_latency,
    scenarios_from_config,
)
from bybit_demo.replay.clock import ReplayClock
from bybit_demo.replay.engine import ReplayEngine, merge_streams
from tests.fixtures.synthetic import scenarios
from tests.helpers import D, delta, demo_config, run_events, snap, trade


def modeled(name: str = "m", **kw: Any) -> ClockScenario:
    return ClockScenario(name, ClockMode.MODELED_AVAILABILITY, ClockBasis.MODELED, "test", **kw)


def published(ev: BookEvent, ts_after_cts: int = 400) -> BookEvent:
    """Give a test event an exchange ts later than its cts."""
    return replace(
        ev, engine_ts_us=ev.exchange_ts_us, system_ts_us=ev.exchange_ts_us + ts_after_cts
    )


def book_with_ts(ts_after_cts: int = 400) -> list[BookEvent]:
    return [
        published(snap(1, [("10", "1")], [("11", "1")], ts=1_000), ts_after_cts),
        published(delta(2, bids=[("10", "2")], ts=2_000), ts_after_cts),
        published(delta(3, bids=[("10", "3")], ts=3_000), ts_after_cts),
    ]


def test_book_is_consumed_at_availability_not_at_cts() -> None:
    clock = ClockSetup(modeled(book_transport_us=50, processing_us=7))
    stamped = list(clock.stamp_book(book_with_ts()))
    assert [e.exchange_ts_us for e in stamped] == [1_000, 2_000, 3_000]  # cts untouched
    assert [e.available_ts_us for e in stamped] == [1_450, 2_450, 3_450]
    assert [e.replay_ts_us for e in stamped] == [1_457, 2_457, 3_457]


def test_trade_before_ts_is_ordered_ahead_of_a_book_message_with_earlier_cts() -> None:
    """cts order would put the book first; availability order must not."""
    book = [published(delta(2, ts=1_000), 400)]  # generated at 1,400
    t = [trade(1_200, Side.BUY, "10", "1")]
    clock = ClockSetup(modeled())
    merged = [ev for _, ev in merge_streams(clock.stamp_book(book), clock.stamp_trades(t))]
    assert [type(e) for e in merged] == [TradeEvent, BookDelta]
    baseline = [ev for _, ev in merge_streams(book, t)]
    assert [type(e) for e in baseline] == [BookDelta, TradeEvent]


def test_equal_ready_time_is_deterministic_trade_first() -> None:
    book = [published(delta(2, ts=500), 500)]  # ready 1,000
    t = [trade(1_000, Side.BUY, "10", "1")]  # ready 1,000
    clock = ClockSetup(modeled())
    merged = [ev for _, ev in merge_streams(clock.stamp_book(book), clock.stamp_trades(t))]
    assert isinstance(merged[0], TradeEvent) and isinstance(merged[1], BookDelta)


def test_negative_delay_is_rejected() -> None:
    with pytest.raises(NegativeLatencyError):
        modeled(book_transport_us=-1)
    with pytest.raises(NegativeLatencyError):
        modeled(processing_us=-1)


def test_event_available_before_it_happened_is_rejected() -> None:
    bad = replace(delta(2, ts=2_000), engine_ts_us=2_000, system_ts_us=1_999)
    with pytest.raises(NegativeLatencyError):
        list(ClockSetup(modeled()).stamp_book([bad]))


def test_absent_timestamps_are_not_fabricated() -> None:
    """Archive records have no receive time; the receipt clock must refuse them."""
    receipt = ClockSetup(
        ClockScenario("r", ClockMode.ACTUAL_RECEIPT, ClockBasis.OBSERVED_THIRD_PARTY, "test")
    )
    with pytest.raises(MissingTimestampError):
        list(receipt.stamp_book(book_with_ts()))
    with pytest.raises(MissingTimestampError):
        list(receipt.stamp_trades([trade(1, Side.BUY, "10", "1")]))
    no_ts = delta(2, ts=1_000)  # system_ts_us is None
    with pytest.raises(MissingTimestampError):
        list(ClockSetup(modeled()).stamp_book([no_ts]))
    with pytest.raises(MissingTimestampError):
        ClockSetup(modeled(use_trade_receipts=True))
    join = ClockSetup(modeled(use_trade_receipts=True), {"other": 5})
    with pytest.raises(MissingTimestampError):
        list(join.stamp_trades([trade(1, Side.BUY, "10", "1", tid="missing")]))


def test_publication_mode_does_not_invent_a_trade_publication_time() -> None:
    s = ClockScenario("p", ClockMode.PUBLICATION_TIME, ClockBasis.PARTIAL, "test")
    clock = ClockSetup(s)
    (ev,) = clock.stamp_trades([trade(1_000, Side.BUY, "10", "1")])
    assert ev.replay_ts_us == 1_000 and ev.local_ts_us is None
    assert "lower bound" in clock.provenance()["fields_used"]["trades"]


def test_fifo_within_a_stream_never_moves_time_backwards() -> None:
    clock = ClockSetup(modeled(use_trade_receipts=True), {"a": 900, "b": 700, "c": 1_000})
    t = [trade(i, Side.BUY, "10", "1", tid=k) for i, k in enumerate("abc")]
    out = list(clock.stamp_trades(t))
    assert [e.replay_ts_us for e in out] == [900, 900, 1_000]
    assert clock.trade_stats.fifo_adjusted == 1
    assert clock.trade_stats.max_fifo_adjustment_us == 200
    assert out[1].local_ts_us == 700  # the recorded value itself is preserved


def test_baseline_clock_returns_events_unchanged() -> None:
    s = ClockScenario("b", ClockMode.CTS_BASELINE, ClockBasis.NON_CAUSAL, "test")
    book = book_with_ts()
    assert list(ClockSetup(s).stamp_book(book)) == book
    with pytest.raises(ConfigError):
        ClockScenario("b", ClockMode.CTS_BASELINE, ClockBasis.NON_CAUSAL, "t", processing_us=1)


# ------------------------------------------------------------ replay-level causality
def _clocked(sc: Any, **kw: Any) -> tuple[list[MarketEvent], list[MarketEvent], ClockSetup]:
    clock = ClockSetup(modeled(**kw))
    book = [replace(e, system_ts_us=e.exchange_ts_us + 30_000) for e in sc.book]
    return list(clock.stamp_book(book)), list(clock.stamp_trades(sc.trades)), clock


def _run(book: Sequence[MarketEvent], trades: Sequence[MarketEvent], **kw: Any) -> MemorySink:
    return run_events([book, trades], **kw)[1]


def test_no_feature_sees_a_book_update_before_its_ready_time() -> None:
    sc = scenarios.long_breakout()
    book, trades, _ = _clocked(sc, book_transport_us=40_000)
    sink = _run(book, trades)
    ready = {e.update_id: e.replay_ts_us for e in book if not isinstance(e, TradeEvent)}
    assert sink.transitions
    for r in sink.transitions:
        assert ready[r.update_id] <= r.ts_us


def test_later_publication_does_not_leak_into_earlier_decisions() -> None:
    """Delay one book message a lot: decisions before its ready time cannot change."""
    sc = scenarios.long_breakout()
    book, trades, _ = _clocked(sc)
    base = _run(book, trades)
    cut = base.transitions[len(base.transitions) // 2].ts_us
    late = [
        replace(e, ready_ts_us=e.replay_ts_us + 10_000_000) if e.replay_ts_us > cut else e
        for e in book
    ]
    moved = _run(late, trades)
    before = [r.to_dict() for r in base.transitions if r.ts_us <= cut]
    assert before == [r.to_dict() for r in moved.transitions if r.ts_us <= cut]


def test_delayed_events_do_not_rewrite_emitted_signals() -> None:
    """Records emitted during a replay are immutable: a late event only adds later records."""
    sc = scenarios.flow_reversal()
    book, trades, _ = _clocked(sc)
    seen: list[dict[str, Any]] = []

    class Snapshotting(MemorySink):
        def on_transition(self, record: Any) -> None:
            super().on_transition(record)
            seen.append(record.to_dict())

    cfg = demo_config()
    sink = Snapshotting()
    ReplayEngine(
        "SYNTHUSDT",
        synchronizer=BookSynchronizer(
            OrderBook("SYNTHUSDT"), BybitContinuityValidator("SYNTHUSDT")
        ),
        features=FeatureEngine(cfg.features, cfg.detector.gates),
        detector=DetectorEngine("SYNTHUSDT", cfg.detector),
        clock=ReplayClock(),
        sinks=[sink],
    ).run(book, trades)
    assert seen == [r.to_dict() for r in sink.transitions]
    times = [r.ts_us for r in sink.transitions]
    assert times == sorted(times)


def test_a_stale_book_message_arriving_late_is_skipped_not_applied() -> None:
    s = BookSynchronizer(OrderBook("TESTUSDT"), BybitContinuityValidator("TESTUSDT"))
    s.process(snap(5, [("10", "1")], [("11", "1")]))
    s.process(delta(6, bids=[("10", "2")]))
    late = s.process(delta(4, bids=[("10", "9")]))
    assert not late.applied and s.book.best_bid() == (D("10"), D("2"))


@pytest.mark.parametrize("delay", [0, 10_000, 250_000])
def test_all_clock_scenarios_are_reproducible(delay: int) -> None:
    sc = scenarios.long_breakout()
    a = _run(*_clocked(sc, book_transport_us=delay, trade_transport_us=delay)[:2])
    b = _run(*_clocked(sc, book_transport_us=delay, trade_transport_us=delay)[:2])
    assert [r.to_dict() for r in a.transitions] == [r.to_dict() for r in b.transitions]


def test_prefix_invariance_when_future_events_are_modified_under_a_clock() -> None:
    sc = scenarios.long_breakout()
    book, trades, _ = _clocked(sc, book_transport_us=20_000, trade_transport_us=20_000)
    base = _run(book, trades)
    cut = base.transitions[len(base.transitions) // 2].ts_us
    falsified = [
        replace(e, side=Side.SELL, quantity=e.quantity * 1000) if e.replay_ts_us > cut else e
        for e in trades
        if isinstance(e, TradeEvent)
    ]
    alt = _run(book, falsified)
    assert [r.to_dict() for r in base.transitions if r.ts_us <= cut] == [
        r.to_dict() for r in alt.transitions if r.ts_us <= cut
    ]


def test_unstamped_replay_equals_a_replay_through_the_baseline_clock() -> None:
    sc = scenarios.long_breakout()
    s = ClockScenario("b", ClockMode.CTS_BASELINE, ClockBasis.NON_CAUSAL, "t")
    clock = ClockSetup(s)
    plain = _run(sc.book, sc.trades)
    through = _run(list(clock.stamp_book(sc.book)), list(clock.stamp_trades(sc.trades)))
    assert [r.to_dict() for r in plain.transitions] == [r.to_dict() for r in through.transitions]


# ------------------------------------------------------------------- configuration
def test_measured_delays_resolve_from_measurements_only() -> None:
    raw = [
        {
            "name": "x",
            "mode": "modeled_availability",
            "basis": "MODELED",
            "description": "d",
            "book_transport_ms": "measured:book_messages.p50_us",
            "trade_transport_ms": 12.5,
        }
    ]
    (s,) = scenarios_from_config(raw, {"book_messages": {"p50_us": 37_000}})
    assert s.book_transport_us == 37_000 and s.trade_transport_us == 12_500
    with pytest.raises(ConfigError):
        scenarios_from_config([{**raw[0], "typo": 1}], {"book_messages": {"p50_us": 1}})


def test_collector_latency_is_measured_per_message(tmp_path: Path) -> None:
    book = tmp_path / "b.csv.gz"
    trades = tmp_path / "t.csv.gz"
    with gzip.open(book, "wt", encoding="utf-8", newline="") as f:
        f.write("timestamp,local_timestamp,price\n100,140,1\n100,140,2\n200,260,1\n")
    with gzip.open(trades, "wt", encoding="utf-8", newline="") as f:
        f.write("timestamp,local_timestamp\n100,90\n")
    m = measure_collector_latency(book, trades)
    assert m["book_messages"]["count"] == 2  # two rows of one message count once
    assert m["trades"]["negative"] == 1  # reported, not hidden
