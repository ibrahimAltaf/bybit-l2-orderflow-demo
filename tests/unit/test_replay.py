from __future__ import annotations

import pytest

from bybit_demo.audit.logger import MemorySink
from bybit_demo.errors import ReplayOrderError
from bybit_demo.models.events import MarketEvent, Side, TradeEvent
from bybit_demo.replay.clock import ReplayClock
from bybit_demo.replay.engine import merge_streams
from tests.fixtures.synthetic import scenarios
from tests.helpers import delta, run_events, snap, trade


def test_merge_is_chronological() -> None:
    book: list[MarketEvent] = [snap(1, [("1", "1")], [("2", "1")], ts=10), delta(2, ts=30)]
    trades = [trade(20, Side.BUY, "2", "1"), trade(40, Side.SELL, "1", "1")]
    ts = [ev.exchange_ts_us for _, ev in merge_streams(book, trades)]
    assert ts == [10, 20, 30, 40]


def test_equal_timestamp_trades_before_book_then_stream_order() -> None:
    book = [delta(2, ts=50), delta(3, ts=50)]
    trades = [trade(50, Side.BUY, "2", "1", tid="a"), trade(50, Side.BUY, "2", "1", tid="b")]
    order = [
        ev.trade_id if isinstance(ev, TradeEvent) else ev.update_id
        for _, ev in merge_streams(book, trades)
    ]
    assert order == ["a", "b", 2, 3]
    # Stream argument order does not change the result (priority, not position, breaks the tie).
    order2 = [
        ev.trade_id if isinstance(ev, TradeEvent) else ev.update_id
        for _, ev in merge_streams(trades, book)
    ]
    assert order2 == order


def test_unsorted_stream_raises() -> None:
    with pytest.raises(ReplayOrderError):
        list(merge_streams([trade(5, Side.BUY, "1", "1"), trade(4, Side.BUY, "1", "1")]))


def test_merge_is_lazy() -> None:
    pulled: list[int] = []

    def gen() -> object:
        for t in (1, 2, 3):
            pulled.append(t)
            yield trade(t, Side.BUY, "1", "1")

    it = merge_streams(gen())  # type: ignore[arg-type]
    next(it)
    assert pulled == [1]  # nothing beyond the current event has been read


def test_clock_monotonic_and_pacing() -> None:
    sleeps: list[float] = []
    now = [0.0]
    clock = ReplayClock(speed=2.0, sleep=sleeps.append, monotonic=lambda: now[0])
    clock.advance(0)
    clock.advance(1_000_000)  # 1 s of event time at 2x -> 0.5 s wall
    assert sleeps == [pytest.approx(0.5)]
    with pytest.raises(ReplayOrderError):
        clock.advance(10)
    fast = ReplayClock(0.0, sleep=lambda _: pytest.fail("fast mode must not sleep"))
    fast.advance(0)
    fast.advance(10**12)


def _transition_tuples(sink: MemorySink) -> list[tuple[object, ...]]:
    return [
        (
            r.ts_us,
            r.direction,
            r.previous_state,
            r.new_state,
            r.reasons,
            r.rejection_flags,
            tuple(sorted(r.features.items())),
        )
        for r in sink.transitions
    ]


def test_deterministic_replay_identical_twice() -> None:
    sc = scenarios.long_breakout()
    s1, k1 = run_events([sc.book, sc.trades])
    s2, k2 = run_events([sc.book, sc.trades])
    assert s1.deterministic_dict() == s2.deterministic_dict()
    assert _transition_tuples(k1) == _transition_tuples(k2)
    assert k1.transitions  # the comparison is not vacuous


def test_no_lookahead_prefix_invariance() -> None:
    """Truncating the input after event N must not change any output produced before N.

    If any feature used a future event, the transitions emitted before the cut
    would differ between the full and the truncated replay.
    """
    sc = scenarios.long_breakout()
    _, full = run_events([sc.book, sc.trades])
    confirm_idx = next(r.event_index for r in full.transitions if r.new_state.value == "CONFIRMED")
    watch_idx = next(r.event_index for r in full.transitions if r.new_state.value == "WATCH")
    for cut in (confirm_idx + 1, confirm_idx - 5, watch_idx + 1):
        _, part = run_events([sc.book, sc.trades], max_events=cut)
        expected = [
            t
            for r, t in zip(full.transitions, _transition_tuples(full), strict=True)
            if r.event_index < cut
        ]
        assert _transition_tuples(part) == expected
        assert expected  # non-vacuous


def test_signal_features_use_only_past_events() -> None:
    """Every transition's features equal a recomputation from events up to its index."""
    sc = scenarios.long_breakout()
    _, full = run_events([sc.book, sc.trades])
    rec = next(r for r in full.transitions if r.new_state.value == "CONFIRMED")
    _, part = run_events([sc.book, sc.trades], max_events=rec.event_index + 1)
    assert part.transitions[-1].features == rec.features


def test_evaluation_waits_for_all_events_at_same_timestamp() -> None:
    """Fills sharing one timestamp (one taker sweep) are evaluated together, never one by one."""
    sc = scenarios.long_breakout()
    t_last = sc.trades[-1].exchange_ts_us
    extra = [
        trade(t_last + 1_000_000 + 7, Side.BUY, "100.01", "1", tid=f"sweep-{i}") for i in range(3)
    ]
    summary, sink = run_events([sc.book, [*sc.trades, *extra]])
    evaluated_at_sweep = [r for r in sink.transitions if r.ts_us == t_last + 1_000_007]
    # Whatever is emitted at the sweep time must see all three fills.
    for r in evaluated_at_sweep:
        assert r.event_index == summary.events - 1
    base, _ = run_events([sc.book, sc.trades])
    # Three extra events at one timestamp add exactly one evaluation (not three).
    assert summary.evaluations == base.evaluations + 1
