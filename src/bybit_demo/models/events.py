"""Normalized, provider-independent market events.

All timestamps are integer microseconds since the Unix epoch (UTC by
definition). Prices and quantities are ``Decimal`` so that book keys and
cumulative volumes are exact and replay is bit-for-bit deterministic.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

_EMPTY: Mapping[str, str] = MappingProxyType({})
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class Side(StrEnum):
    """Aggressor (taker) side of a trade, or side of the book."""

    BUY = "Buy"
    SELL = "Sell"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1


class AggressorSource(StrEnum):
    """Provenance of a trade's aggressor side."""

    REPORTED = "reported"  # exchange/provider supplied the taker side
    INFERRED = "inferred"  # derived by an explicit inference rule (e.g. tick rule)


class EventKind(StrEnum):
    TRADE = "trade"
    BOOK_SNAPSHOT = "book_snapshot"
    BOOK_DELTA = "book_delta"


def us_to_iso(ts_us: int) -> str:
    """Render epoch microseconds as an ISO-8601 UTC string with microseconds.

    Integer arithmetic only: ``ts_us / 1e6`` as a float has ~0.5 us resolution at
    current epoch values, so it is not used for rendering evidence timestamps.
    """
    return (_EPOCH + timedelta(microseconds=ts_us)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def iso_to_us(value: str) -> int:
    """Parse an ISO-8601 timestamp that carries an explicit offset (e.g. ``Z``) to epoch us."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"timestamp {value!r} must carry a UTC offset")
    return (dt - _EPOCH) // timedelta(microseconds=1)


@dataclass(frozen=True, slots=True)
class PriceLevel:
    """One price level. ``quantity == 0`` in a delta means delete."""

    price: Decimal
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class TradeEvent:
    """A single public trade (one fill against resting liquidity)."""

    exchange: str
    symbol: str
    exchange_ts_us: int  # matching-engine fill time (Bybit ``T``)
    price: Decimal
    quantity: Decimal
    side: Side  # aggressor / taker side
    aggressor_source: AggressorSource
    trade_id: str | None = None
    local_ts_us: int | None = None  # capture time, when the provider records one
    provider: str = ""
    raw: Mapping[str, str] = field(default=_EMPTY, compare=False)

    kind: EventKind = field(default=EventKind.TRADE, init=False)

    @property
    def notional(self) -> Decimal:
        return self.price * self.quantity

    @property
    def signed_quantity(self) -> Decimal:
        return self.quantity if self.side is Side.BUY else -self.quantity


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    """Full replacement of the local book.

    ``update_id`` / ``cross_seq`` are Bybit's ``u`` / ``seq`` when present;
    providers that do not publish them (e.g. Tardis CSV) leave them ``None``.
    """

    exchange: str
    symbol: str
    exchange_ts_us: int  # time used for ordering (see BookClock in config)
    bids: tuple[PriceLevel, ...]
    asks: tuple[PriceLevel, ...]
    update_id: int | None = None
    cross_seq: int | None = None
    system_ts_us: int | None = None  # Bybit ``ts`` (publish time)
    engine_ts_us: int | None = None  # Bybit ``cts`` (matching engine time)
    local_ts_us: int | None = None
    depth: int | None = None
    provider: str = ""
    source_line: int | None = None

    kind: EventKind = field(default=EventKind.BOOK_SNAPSHOT, init=False)


@dataclass(frozen=True, slots=True)
class BookDelta:
    """Incremental change to the local book."""

    exchange: str
    symbol: str
    exchange_ts_us: int
    bid_updates: tuple[PriceLevel, ...]
    ask_updates: tuple[PriceLevel, ...]
    update_id: int | None = None
    cross_seq: int | None = None
    system_ts_us: int | None = None
    engine_ts_us: int | None = None
    local_ts_us: int | None = None
    depth: int | None = None
    provider: str = ""
    source_line: int | None = None

    kind: EventKind = field(default=EventKind.BOOK_DELTA, init=False)


BookEvent = BookSnapshot | BookDelta
MarketEvent = TradeEvent | BookSnapshot | BookDelta
