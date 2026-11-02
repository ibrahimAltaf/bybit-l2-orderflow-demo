"""Deterministic chronological replay.

Ordering key for every event: ``(time_us, kind_priority, stream_index, position_in_stream)``.

* ``time_us`` -- trade fill time ``T`` and the configured book clock
  (``cts`` by default: the matching-engine time, i.e. the same clock as ``T``).
* ``kind_priority`` -- at an identical microsecond, trades (0) are processed
  before book updates (1). A Bybit book delta stamped ``cts`` already reflects
  fills up to that time, so a fill with ``T == cts`` is processed first and
  can be attributed to the level decrease that delta reports.
* the last two components make ties fully deterministic.

The engine pulls one event at a time from lazy, per-stream iterators
(``heapq.merge``) and updates state with it. The detector is evaluated once per
distinct timestamp, after *all* events carrying that timestamp are applied
(they are simultaneously available, e.g. the fills of one taker sweep). To find
the end of a timestamp batch the engine looks at the next event's timestamp
only; no feature or rule can read an event before the clock has reached it.
"""

from __future__ import annotations

import heapq
import time
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, fields
from decimal import Decimal
from typing import Any, Protocol

from bybit_demo.book.continuity import BookSynchronizer, ContinuityIssue
from bybit_demo.detector.engine import DetectorEngine
from bybit_demo.errors import ReplayOrderError
from bybit_demo.features.feature_engine import FeatureEngine
from bybit_demo.models.events import MarketEvent, TradeEvent, iso_to_us, us_to_iso
from bybit_demo.models.signals import SignalState, TransitionRecord
from bybit_demo.replay.clock import ReplayClock

_PRIORITY_TRADE = 0
_PRIORITY_BOOK = 1

OrderKey = tuple[int, int, int, int]


def event_priority(ev: MarketEvent) -> int:
    return _PRIORITY_TRADE if isinstance(ev, TradeEvent) else _PRIORITY_BOOK


def _keyed(
    stream: Iterable[MarketEvent], stream_index: int
) -> Iterator[tuple[OrderKey, MarketEvent]]:
    last = None
    for pos, ev in enumerate(stream):
        t = ev.exchange_ts_us
        if last is not None and t < last:
            raise ReplayOrderError(
                f"stream {stream_index} not chronological at position {pos}: {t} < {last}"
            )
        last = t
        yield (t, event_priority(ev), stream_index, pos), ev


def merge_streams(*streams: Iterable[MarketEvent]) -> Iterator[tuple[OrderKey, MarketEvent]]:
    """Lazily merge individually-sorted streams into one deterministic sequence."""
    return heapq.merge(*(_keyed(s, i) for i, s in enumerate(streams)), key=lambda kv: kv[0])


class ReplaySink(Protocol):
    def on_transition(self, record: TransitionRecord) -> None: ...

    def on_continuity(self, issue: ContinuityIssue) -> None: ...


@dataclass(slots=True)
class ReplaySummary:
    symbol: str
    events: int = 0
    trades: int = 0
    book_events: int = 0
    evaluations: int = 0
    first_ts_us: int | None = None
    last_ts_us: int | None = None
    snapshots: int = 0
    deltas_applied: int = 0
    deltas_skipped: int = 0
    sequence_gaps: int = 0
    crossed_books: int = 0
    checkpoint_comparisons: int = 0
    checkpoint_matches: int = 0
    continuity_issue_counts: dict[str, int] = field(default_factory=dict)
    final_best_bid: str | None = None
    final_best_ask: str | None = None
    final_bid_levels: int = 0
    final_ask_levels: int = 0
    buy_notional: str = "0"
    sell_notional: str = "0"
    buy_qty: str = "0"
    sell_qty: str = "0"
    cvd_notional: str = "0"
    cvd_qty: str = "0"
    buy_trades: int = 0
    sell_trades: int = 0
    inferred_side_trades: int = 0
    transitions: int = 0
    transition_counts: dict[str, int] = field(default_factory=dict)
    rejection_counts: dict[str, int] = field(default_factory=dict)
    confirmed: int = 0
    wall_time_s: float = 0.0  # excluded from determinism comparison

    def deterministic_dict(self) -> dict[str, Any]:
        d = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "wall_time_s"}
        d["first_ts"] = us_to_iso(self.first_ts_us) if self.first_ts_us else None
        d["last_ts"] = us_to_iso(self.last_ts_us) if self.last_ts_us else None
        return d


def _parse_utc(value: str | None) -> int | None:
    return None if value is None else iso_to_us(value)


class ReplayEngine:
    """Wires synchronizer -> features -> detector for one symbol (dependency-injected)."""

    def __init__(
        self,
        symbol: str,
        *,
        synchronizer: BookSynchronizer,
        features: FeatureEngine,
        detector: DetectorEngine,
        clock: ReplayClock,
        sinks: Iterable[ReplaySink] = (),
    ) -> None:
        self.symbol = symbol
        self.sync = synchronizer
        self.features = features
        self.detector = detector
        self.clock = clock
        self.sinks = list(sinks)

    def run(
        self,
        *streams: Iterable[MarketEvent],
        max_events: int | None = None,
        start_utc: str | None = None,
        end_utc: str | None = None,
    ) -> ReplaySummary:
        """Replay all streams to completion (or ``max_events`` / ``end_utc``)."""
        t0 = time.perf_counter()
        start_us, end_us = _parse_utc(start_utc), _parse_utc(end_utc)
        summary = ReplaySummary(self.symbol)
        tcount: Counter[str] = Counter()
        rcount: Counter[str] = Counter()

        merged = merge_streams(*streams)
        nxt = next(merged, None)
        idx = -1
        while nxt is not None:
            idx += 1
            if max_events is not None and idx >= max_events:
                break
            _, ev = nxt
            t = ev.exchange_ts_us
            if end_us is not None and t > end_us:
                break
            self.clock.advance(t)
            summary.events += 1
            summary.first_ts_us = summary.first_ts_us or t
            summary.last_ts_us = t

            if isinstance(ev, TradeEvent):
                summary.trades += 1
                self.features.on_trade(ev)
            else:
                summary.book_events += 1
                update = self.sync.process(ev)
                self.features.on_book(t, update, self.sync.book, self.sync.usable)
                for issue in update.issues:
                    for s in self.sinks:
                        s.on_continuity(issue)

            # Evaluate once per timestamp, after every event stamped ``t`` has been
            # applied (e.g. all fills of one taker sweep). Only the next event's
            # timestamp is inspected to find the batch end -- never its content.
            nxt = next(merged, None)
            if nxt is not None and nxt[1].exchange_ts_us == t:
                continue
            if start_us is not None and t < start_us:
                continue
            fv = self.features.compute(
                t, idx, self.sync.book, synced=self.sync.validator.synced, crossed=self.sync.crossed
            )
            if fv is None:
                continue
            summary.evaluations += 1
            for rec in self.detector.evaluate(fv):
                summary.transitions += 1
                tcount[
                    f"{rec.direction.value} {rec.previous_state.value}->{rec.new_state.value}"
                ] += 1
                for flag in rec.rejection_flags:
                    rcount[flag.value] += 1
                if rec.new_state is SignalState.CONFIRMED:
                    summary.confirmed += 1
                for s in self.sinks:
                    s.on_transition(rec)

        self._finalize(summary, tcount, rcount)
        summary.wall_time_s = round(time.perf_counter() - t0, 3)
        return summary

    def _finalize(self, s: ReplaySummary, tcount: Counter[str], rcount: Counter[str]) -> None:
        st = self.sync.stats
        s.snapshots = st.snapshots
        s.deltas_applied = st.deltas_applied
        s.deltas_skipped = st.deltas_skipped
        s.checkpoint_comparisons = st.checkpoint_comparisons
        s.checkpoint_matches = st.checkpoint_matches
        s.continuity_issue_counts = dict(sorted(st.issue_counts.items()))
        s.sequence_gaps = st.issue_counts.get("SEQUENCE_GAP", 0)
        s.crossed_books = st.issue_counts.get("CROSSED_BOOK", 0)
        top = self.sync.book.top()
        if top is not None:
            s.final_best_bid, s.final_best_ask = str(top.best_bid), str(top.best_ask)
        s.final_bid_levels, s.final_ask_levels = self.sync.book.level_counts()
        c = self.features.cvd
        s.buy_notional, s.sell_notional = _dec(c.buy_notional), _dec(c.sell_notional)
        s.buy_qty, s.sell_qty = _dec(c.buy_qty), _dec(c.sell_qty)
        s.cvd_notional, s.cvd_qty = _dec(c.cvd_notional), _dec(c.cvd_qty)
        s.buy_trades, s.sell_trades = c.buy_count, c.sell_count
        s.inferred_side_trades = c.inferred_count
        s.transition_counts = dict(sorted(tcount.items()))
        s.rejection_counts = dict(sorted(rcount.items()))


def _dec(d: Decimal) -> str:
    return format(d.normalize(), "f") if d != 0 else "0"
