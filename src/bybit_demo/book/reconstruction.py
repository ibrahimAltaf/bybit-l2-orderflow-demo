"""Local incremental Level-2 order book.

The book itself is provider-agnostic: it only knows how to apply a snapshot
(full replace) and a delta (insert / update / delete-on-zero). Whether a given
delta *should* be applied is decided by a provider-specific continuity
validator (see :mod:`bybit_demo.book.continuity`).
"""

from __future__ import annotations

from bisect import bisect_left, insort
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from decimal import Decimal

from bybit_demo.errors import BookNotInitializedError
from bybit_demo.models.events import BookDelta, BookSnapshot, PriceLevel
from bybit_demo.models.orderbook import BandDepth, BookSide, LevelChange, TopOfBook

_ZERO = Decimal(0)


class _Ladder:
    """One side of the book: ``price -> qty`` plus an ascending price list."""

    __slots__ = ("_prices", "_qty")

    def __init__(self) -> None:
        self._qty: dict[Decimal, Decimal] = {}
        self._prices: list[Decimal] = []

    def clear(self) -> None:
        self._qty.clear()
        self._prices.clear()

    def __len__(self) -> int:
        return len(self._prices)

    def get(self, price: Decimal) -> Decimal:
        return self._qty.get(price, _ZERO)

    def set(self, price: Decimal, qty: Decimal) -> Decimal:
        """Set ``price`` to ``qty`` (0 deletes). Returns the previous quantity."""
        old = self._qty.get(price, _ZERO)
        if qty == 0:
            if old != 0:
                del self._qty[price]
                idx = bisect_left(self._prices, price)
                del self._prices[idx]
            return old
        if old == 0:
            insort(self._prices, price)
        self._qty[price] = qty
        return old

    def ascending(self) -> Iterator[tuple[Decimal, Decimal]]:
        for p in self._prices:
            yield p, self._qty[p]

    def descending(self) -> Iterator[tuple[Decimal, Decimal]]:
        for p in reversed(self._prices):
            yield p, self._qty[p]

    def lowest(self) -> Decimal | None:
        return self._prices[0] if self._prices else None

    def highest(self) -> Decimal | None:
        return self._prices[-1] if self._prices else None

    def as_dict(self) -> dict[Decimal, Decimal]:
        return dict(self._qty)


@dataclass(frozen=True, slots=True)
class SnapshotComparison:
    """Result of comparing the reconstructed book to an exchange snapshot."""

    bid_levels_compared: int
    ask_levels_compared: int
    bid_mismatches: int
    ask_mismatches: int

    @property
    def matches(self) -> bool:
        return self.bid_mismatches == 0 and self.ask_mismatches == 0


class OrderBook:
    """Incremental L2 book for one symbol.

    Bids are exposed best-first (descending), asks best-first (ascending).
    """

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self._bids = _Ladder()
        self._asks = _Ladder()
        self._initialized = False
        self.last_update_id: int | None = None
        self.last_cross_seq: int | None = None
        self.last_event_ts_us: int | None = None
        self.snapshots_applied = 0
        self.deltas_applied = 0

    # ------------------------------------------------------------------ state
    @property
    def is_initialized(self) -> bool:
        return self._initialized

    def invalidate(self) -> None:
        """Mark the book unusable (e.g. after a sequence gap) until the next snapshot."""
        self._initialized = False

    def apply_snapshot(self, snapshot: BookSnapshot) -> None:
        self._bids.clear()
        self._asks.clear()
        for lvl in snapshot.bids:
            self._bids.set(lvl.price, lvl.quantity)
        for lvl in snapshot.asks:
            self._asks.set(lvl.price, lvl.quantity)
        self._initialized = True
        self._stamp(snapshot.update_id, snapshot.cross_seq, snapshot.exchange_ts_us)
        self.snapshots_applied += 1

    def apply_delta(self, delta: BookDelta) -> list[LevelChange]:
        """Apply a delta and return the per-level changes it caused."""
        if not self._initialized:
            raise BookNotInitializedError(f"{self.symbol}: delta before valid snapshot")
        changes = self._apply_side(self._bids, BookSide.BID, delta.bid_updates)
        changes += self._apply_side(self._asks, BookSide.ASK, delta.ask_updates)
        self._stamp(delta.update_id, delta.cross_seq, delta.exchange_ts_us)
        self.deltas_applied += 1
        return changes

    @staticmethod
    def _apply_side(
        ladder: _Ladder, side: BookSide, updates: Iterable[PriceLevel]
    ) -> list[LevelChange]:
        out: list[LevelChange] = []
        for lvl in updates:
            old = ladder.set(lvl.price, lvl.quantity)
            if old != lvl.quantity:
                out.append(LevelChange(side, lvl.price, old, lvl.quantity))
        return out

    def _stamp(self, update_id: int | None, cross_seq: int | None, ts_us: int) -> None:
        self.last_update_id = update_id
        self.last_cross_seq = cross_seq
        self.last_event_ts_us = ts_us

    # ---------------------------------------------------------------- queries
    def best_bid(self) -> tuple[Decimal, Decimal] | None:
        p = self._bids.highest()
        return None if p is None else (p, self._bids.get(p))

    def best_ask(self) -> tuple[Decimal, Decimal] | None:
        p = self._asks.lowest()
        return None if p is None else (p, self._asks.get(p))

    def top(self) -> TopOfBook | None:
        bb, ba = self.best_bid(), self.best_ask()
        if bb is None or ba is None:
            return None
        return TopOfBook(bb[0], bb[1], ba[0], ba[1])

    def is_crossed(self) -> bool:
        bb, ba = self._bids.highest(), self._asks.lowest()
        return bb is not None and ba is not None and bb >= ba

    def bids(self) -> Iterator[tuple[Decimal, Decimal]]:
        return self._bids.descending()

    def asks(self) -> Iterator[tuple[Decimal, Decimal]]:
        return self._asks.ascending()

    def quantity_at(self, side: BookSide, price: Decimal) -> Decimal:
        return (self._bids if side is BookSide.BID else self._asks).get(price)

    def level_counts(self) -> tuple[int, int]:
        return len(self._bids), len(self._asks)

    def depth_within_bps(self, bps: float) -> BandDepth:
        """Resting liquidity within ``bps`` of mid on each side."""
        return self.depth_bands((bps,))[0]

    def depth_bands(self, bands_bps: tuple[float, ...]) -> tuple[BandDepth, ...]:
        """Depth for several bands in one walk of each side (bands in any order)."""
        top = self.top()
        if top is None:
            return tuple(BandDepth(b, _ZERO, _ZERO, _ZERO, _ZERO) for b in bands_bps)
        mid = top.mid
        order = sorted(range(len(bands_bps)), key=lambda i: bands_bps[i])
        offsets = [mid * Decimal(str(bands_bps[i])) / Decimal(10_000) for i in order]
        bids = self._cumulate(self._bids.descending(), [mid - o for o in offsets], below=True)
        asks = self._cumulate(self._asks.ascending(), [mid + o for o in offsets], below=False)
        out: list[BandDepth | None] = [None] * len(bands_bps)
        for rank, i in enumerate(order):
            (bq, bn), (aq, an) = bids[rank], asks[rank]
            out[i] = BandDepth(bands_bps[i], bq, aq, bn, an)
        return tuple(b for b in out if b is not None)

    @staticmethod
    def _cumulate(
        levels: Iterator[tuple[Decimal, Decimal]], limits: list[Decimal], *, below: bool
    ) -> list[tuple[Decimal, Decimal]]:
        """Cumulative (qty, notional) up to each limit; limits ordered nearest-first."""
        res: list[tuple[Decimal, Decimal]] = []
        q_sum = n_sum = _ZERO
        k = 0
        for p, q in levels:
            while k < len(limits) and (p < limits[k] if below else p > limits[k]):
                res.append((q_sum, n_sum))
                k += 1
            if k == len(limits):
                break
            q_sum += q
            n_sum += p * q
        while len(res) < len(limits):
            res.append((q_sum, n_sum))
        return res

    def compare_to_snapshot(self, snapshot: BookSnapshot) -> SnapshotComparison:
        """Level-by-level comparison against an exchange snapshot.

        Only the snapshot's depth window is compared: a depth-limited stream
        (e.g. ob200) is authoritative only for the levels it publishes.
        """
        bid_mm = self._compare_side(self._bids, snapshot.bids, descending=True)
        ask_mm = self._compare_side(self._asks, snapshot.asks, descending=False)
        return SnapshotComparison(len(snapshot.bids), len(snapshot.asks), bid_mm, ask_mm)

    @staticmethod
    def _compare_side(ladder: _Ladder, levels: tuple[PriceLevel, ...], *, descending: bool) -> int:
        ours = list(ladder.descending() if descending else ladder.ascending())[: len(levels)]
        theirs = [(lvl.price, lvl.quantity) for lvl in levels]
        mismatches = sum(1 for a, b in zip(ours, theirs, strict=False) if a != b)
        return mismatches + abs(len(ours) - len(theirs))
