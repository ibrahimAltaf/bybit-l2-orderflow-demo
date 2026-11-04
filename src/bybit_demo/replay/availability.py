"""Availability clocks: when could a consumer actually have had each event?

The original replay orders book records by ``cts``. Bybit documents ``cts`` as "the
timestamp from the matching engine when this orderbook data is produced" and the
message ``ts`` as "the timestamp (ms) that the system generates the data". On the
verified files ``ts`` is up to 399 ms later than ``cts``, so a book record applied at
``cts`` is applied before the message existed. That baseline is kept for comparison
and is labelled non-causal; these clocks replace it.

What each source really contains:

* Bybit book archive: ``cts`` and ``ts``. No receive time.
* Bybit trade archive: fill time only. No publication time and no receive time.
* Tardis.dev CSV: exchange ``timestamp`` plus ``local_timestamp`` ("message arrival
  timestamp"), recorded by a third-party collector on its own clock, not by this bot.

Rules enforced here:

* A timestamp the source lacks is never fabricated: a clock that needs one raises
  :class:`MissingTimestampError`. Modeled delays are explicit and labelled modeled.
* Delays must be non-negative, and no event may become available before it happened
  (:class:`NegativeLatencyError`).
* One stream is one ordered connection, so availability is FIFO within a stream:
  ``ready = max(ready, previous ready)``. Adjustments are counted and reported.
* ``cts_baseline`` returns events untouched, which keeps its output byte-identical.
"""

from __future__ import annotations

import csv
import gzip
import io
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from bybit_demo.errors import ConfigError, MissingTimestampError, NegativeLatencyError
from bybit_demo.models.events import BookEvent, TradeEvent

_MS = 1_000


class ClockMode(StrEnum):
    CTS_BASELINE = "cts_baseline"  # matching-engine order; NON-CAUSAL, comparison only
    PUBLICATION_TIME = "publication_time"  # book at ts; trades have no publication field
    MODELED_AVAILABILITY = "modeled_availability"  # documented non-negative delays
    ACTUAL_RECEIPT = "actual_receipt"  # recorded receive timestamps only


class ClockBasis(StrEnum):
    """How much of a scenario's clock is measured rather than assumed."""

    NON_CAUSAL = "NON_CAUSAL_BASELINE"
    PARTIAL = "PARTIAL_PUBLICATION_LOWER_BOUND"
    MODELED = "MODELED"
    OBSERVED_THIRD_PARTY = "OBSERVED_AT_THIRD_PARTY_COLLECTOR"


@dataclass(frozen=True, slots=True)
class ClockScenario:
    name: str
    mode: ClockMode
    basis: ClockBasis
    description: str
    book_transport_us: int = 0  # added to ts
    trade_publication_us: int = 0  # added to T
    trade_transport_us: int = 0  # added to T + publication
    processing_us: int = 0  # available -> ready, both streams
    use_trade_receipts: bool = False  # trades arrive at a recorded third-party receive time
    dataset: str | None = None  # default: the primary dataset

    def __post_init__(self) -> None:
        delays = (
            self.book_transport_us,
            self.trade_publication_us,
            self.trade_transport_us,
            self.processing_us,
        )
        if any(d < 0 for d in delays):
            raise NegativeLatencyError(f"{self.name}: negative delay in {delays}")
        if self.mode is ClockMode.CTS_BASELINE and any(delays):
            raise ConfigError(f"{self.name}: cts_baseline takes no delays")
        if self.use_trade_receipts and self.mode is not ClockMode.MODELED_AVAILABILITY:
            raise ConfigError(f"{self.name}: trade receipts apply to modeled_availability only")


@dataclass(slots=True)
class StreamClockStats:
    events: int = 0
    fifo_adjusted: int = 0
    max_fifo_adjustment_us: int = 0


def _fifo(events: Iterable[tuple[Any, int, int]], stats: StreamClockStats) -> Iterator[Any]:
    """Stamp ``(event, available, ready)`` keeping ``ready`` non-decreasing in stream order."""
    last_ready: int | None = None
    for ev, available, raw_ready in events:
        stats.events += 1
        ready = raw_ready
        if last_ready is not None and raw_ready < last_ready:
            stats.fifo_adjusted += 1
            stats.max_fifo_adjustment_us = max(stats.max_fifo_adjustment_us, last_ready - raw_ready)
            ready = last_ready
        last_ready = ready
        yield replace(ev, available_ts_us=available, ready_ts_us=ready)


@dataclass(slots=True)
class ClockSetup:
    """A scenario plus the data it needs; stamps both streams of one replay."""

    scenario: ClockScenario
    trade_receipts: Mapping[str, int] | None = None
    measurements: dict[str, Any] = field(default_factory=dict)
    book_stats: StreamClockStats = field(default_factory=StreamClockStats)
    trade_stats: StreamClockStats = field(default_factory=StreamClockStats)

    def __post_init__(self) -> None:
        if self.scenario.use_trade_receipts and not self.trade_receipts:
            raise MissingTimestampError(f"{self.scenario.name}: needs recorded trade receipts")

    # ------------------------------------------------------------------ book
    def _book_available(self, ev: BookEvent) -> int:
        s = self.scenario
        if s.mode is ClockMode.ACTUAL_RECEIPT:
            if ev.local_ts_us is None:
                raise MissingTimestampError(
                    f"{s.name}: book record {ev.source_line} has no recorded receive time"
                )
            return ev.local_ts_us
        if ev.system_ts_us is None:
            raise MissingTimestampError(
                f"{s.name}: book record {ev.source_line} has no publication field (ts)"
            )
        return ev.system_ts_us + s.book_transport_us

    def stamp_book(self, stream: Iterable[BookEvent]) -> Iterator[BookEvent]:
        if self.scenario.mode is ClockMode.CTS_BASELINE:
            return iter(stream)

        def stamped() -> Iterator[tuple[BookEvent, int, int]]:
            for ev in stream:
                available = self._book_available(ev)
                matched = ev.engine_ts_us if ev.engine_ts_us is not None else ev.exchange_ts_us
                if available < matched:
                    raise NegativeLatencyError(
                        f"{self.scenario.name}: book record {ev.source_line} available "
                        f"{matched - available} us before it was produced"
                    )
                yield ev, available, available + self.scenario.processing_us

        return _fifo(stamped(), self.book_stats)

    # ---------------------------------------------------------------- trades
    def _trade_available(self, ev: TradeEvent) -> tuple[int, int | None]:
        """(available time, recorded receive time if any)."""
        s = self.scenario
        if s.mode is ClockMode.ACTUAL_RECEIPT:
            if ev.local_ts_us is None:
                raise MissingTimestampError(f"{s.name}: trade {ev.trade_id} has no receive time")
            return ev.local_ts_us, ev.local_ts_us
        if s.mode is ClockMode.PUBLICATION_TIME:
            # The archive has no trade publication time. T is a lower bound and is
            # reported as such, never presented as a publication time.
            return ev.exchange_ts_us, None
        if s.use_trade_receipts:
            assert self.trade_receipts is not None
            received = self.trade_receipts.get(ev.trade_id or "")
            if received is None:
                raise MissingTimestampError(f"{s.name}: no receipt for trade {ev.trade_id}")
            return received + s.trade_transport_us, received
        return ev.exchange_ts_us + s.trade_publication_us + s.trade_transport_us, None

    def stamp_trades(self, stream: Iterable[TradeEvent]) -> Iterator[TradeEvent]:
        if self.scenario.mode is ClockMode.CTS_BASELINE:
            return iter(stream)

        def stamped() -> Iterator[tuple[TradeEvent, int, int]]:
            for ev in stream:
                available, received = self._trade_available(ev)
                if available < ev.exchange_ts_us:
                    # Tardis exchange timestamps are ms-truncated; Bybit's are 0.1 ms, so
                    # a real negative value here would mean a clock or join error.
                    raise NegativeLatencyError(
                        f"{self.scenario.name}: trade {ev.trade_id} available "
                        f"{ev.exchange_ts_us - available} us before its fill"
                    )
                ready = available + self.scenario.processing_us
                yield replace(ev, local_ts_us=received), available, ready

        return _fifo(stamped(), self.trade_stats)

    # ------------------------------------------------------------- reporting
    def provenance(self) -> dict[str, Any]:
        d = asdict(self.scenario)
        d["mode"], d["basis"] = self.scenario.mode.value, self.scenario.basis.value
        d["measurements"] = self.measurements
        d["fields_used"] = _fields_used(self.scenario)
        return d

    def report(self) -> dict[str, Any]:
        return {
            "scenario": self.provenance(),
            "book_stream": asdict(self.book_stats),
            "trade_stream": asdict(self.trade_stats),
        }


def _fields_used(s: ClockScenario) -> dict[str, str]:
    if s.mode is ClockMode.CTS_BASELINE:
        return {"book": "cts", "trades": "T"}
    if s.mode is ClockMode.ACTUAL_RECEIPT:
        return {"book": "local_timestamp", "trades": "local_timestamp"}
    book = f"ts + {s.book_transport_us} us"
    if s.mode is ClockMode.PUBLICATION_TIME:
        return {"book": book, "trades": "T (lower bound: no publication field exists)"}
    if s.use_trade_receipts:
        return {"book": book, "trades": "third-party local_timestamp joined by trade ID"}
    return {"book": book, "trades": f"T + {s.trade_publication_us + s.trade_transport_us} us"}


# ------------------------------------------------------------ third-party receipts
def load_trade_receipts(path: Path) -> dict[str, int]:
    """Trade ID -> recorded ``local_timestamp`` from a Tardis.dev trades CSV."""
    with gzip.open(path, "rb") as gz, io.TextIOWrapper(gz, encoding="utf-8", newline="") as fh:
        return {row["id"]: int(row["local_timestamp"]) for row in csv.DictReader(fh)}


def _nearest_rank(values: list[int], q: float) -> int:
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))]


def measure_collector_latency(book_path: Path, trades_path: Path) -> dict[str, Any]:
    """``local_timestamp - timestamp`` per message at the third-party collector.

    This is exchange-clock to collector-clock distance. The two clocks are not known
    to be synchronized, so it is an empirical calibration, not a true one-way latency.
    """
    book: list[int] = []
    previous: tuple[str, str] | None = None
    with (
        gzip.open(book_path, "rb") as gz,
        io.TextIOWrapper(gz, encoding="utf-8", newline="") as fh,
    ):
        for row in csv.DictReader(fh):
            key = (row["timestamp"], row["local_timestamp"])
            if key != previous:  # one message spans many CSV rows
                book.append(int(key[1]) - int(key[0]))
                previous = key
    trades: list[int] = []
    with (
        gzip.open(trades_path, "rb") as gz,
        io.TextIOWrapper(gz, encoding="utf-8", newline="") as fh,
    ):
        trades = [int(r["local_timestamp"]) - int(r["timestamp"]) for r in csv.DictReader(fh)]

    def stats(v: list[int]) -> dict[str, int]:
        return {
            "count": len(v),
            "min_us": min(v),
            "p50_us": _nearest_rank(v, 0.5),
            "p99_us": _nearest_rank(v, 0.99),
            "max_us": max(v),
            "negative": sum(1 for x in v if x < 0),
        }

    return {
        "definition": "local_timestamp - exchange timestamp, per message, Tardis.dev collector",
        "caveat": (
            "Collector and exchange clocks are not known to be synchronized; the collector "
            "is a third party (documented: GCP asia-northeast1), not this bot. The book "
            "stream measured is orderbook.50, not the archive's level 200."
        ),
        "book_messages": stats(book),
        "trades": stats(trades),
    }


# ---------------------------------------------------------------- scenario config
def _delay(value: Any, measured: dict[str, Any]) -> int:
    """A delay in ms, or the name of a measurement such as ``measured:book_messages.p50_us``."""
    if isinstance(value, str) and value.startswith("measured:"):
        group, key = value.removeprefix("measured:").split(".")
        return int(measured[group][key])
    return int(float(value) * _MS)


def scenarios_from_config(
    raw: list[dict[str, Any]], measured: dict[str, Any]
) -> list[ClockScenario]:
    out = []
    for item in raw:
        cfg = dict(item)
        out.append(
            ClockScenario(
                name=cfg.pop("name"),
                mode=ClockMode(cfg.pop("mode")),
                basis=ClockBasis(cfg.pop("basis")),
                description=cfg.pop("description"),
                book_transport_us=_delay(cfg.pop("book_transport_ms", 0), measured),
                trade_publication_us=_delay(cfg.pop("trade_publication_ms", 0), measured),
                trade_transport_us=_delay(cfg.pop("trade_transport_ms", 0), measured),
                processing_us=_delay(cfg.pop("processing_ms", 0), measured),
                use_trade_receipts=bool(cfg.pop("use_trade_receipts", False)),
                dataset=cfg.pop("dataset", None),
            )
        )
        if cfg:
            raise ConfigError(f"{item['name']}: unknown scenario keys {sorted(cfg)}")
    return out
