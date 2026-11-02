"""Order-book state metrics and near-touch liquidity-flow accounting.

Execution attribution (how a level decrease is split into *executed* vs
*cancelled*) is an approximation that must be disclosed: Bybit's ob200 stream
is pushed every 100 ms (documented), so adds and cancels at the same price
inside one interval net out and are invisible. A decrease is attributed to
execution only up to the quantity of trades that actually printed at that
price on that side since the previous book update; the remainder is counted
as cancellation (withdrawal).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from bybit_demo.book.reconstruction import OrderBook
from bybit_demo.flow.rolling import TimeWindowSum
from bybit_demo.models.events import Side, TradeEvent
from bybit_demo.models.orderbook import BandDepth, BookSide, LevelChange, TopOfBook

_ZERO = Decimal(0)
_BPS = Decimal(10_000)


@dataclass(frozen=True, slots=True)
class BookMetrics:
    top: TopOfBook
    mid: Decimal
    spread_bps: float
    microprice: Decimal
    microprice_disp_bps: float  # (microprice - mid) / mid in bps
    microprice_rel: float  # (microprice - mid) / spread, in [-0.5, 0.5]
    bands: tuple[BandDepth, ...]
    bid_levels: int
    ask_levels: int

    def band(self, bps: float) -> BandDepth:
        for b in self.bands:
            if b.bps == bps:
                return b
        raise KeyError(f"depth band {bps} bps not configured")


def compute_book_metrics(book: OrderBook, bands_bps: tuple[float, ...]) -> BookMetrics | None:
    """Snapshot metrics of the current book; ``None`` if either side is empty."""
    top = book.top()
    if top is None:
        return None
    mid = top.mid
    micro = top.microprice
    spread = top.spread
    nb, na = book.level_counts()
    return BookMetrics(
        top=top,
        mid=mid,
        spread_bps=top.spread_bps,
        microprice=micro,
        microprice_disp_bps=float((micro - mid) / mid * _BPS),
        microprice_rel=0.0 if spread == 0 else float((micro - mid) / spread),
        bands=book.depth_bands(bands_bps),
        bid_levels=nb,
        ask_levels=na,
    )


class _SideFlow:
    __slots__ = ("added", "cancelled", "executed", "replenished")

    def __init__(self, window_us: int) -> None:
        self.added = TimeWindowSum(window_us)
        self.cancelled = TimeWindowSum(window_us)
        self.executed = TimeWindowSum(window_us)
        self.replenished = TimeWindowSum(window_us)

    def advance(self, now_us: int) -> None:
        for w in (self.added, self.cancelled, self.executed, self.replenished):
            w.advance(now_us)


@dataclass(frozen=True, slots=True)
class SideLiquidityFlow:
    """Near-touch liquidity flow for one book side over the window (USDT notional)."""

    added: Decimal
    cancelled: Decimal
    executed: Decimal
    replenished: Decimal
    window_s: float

    @property
    def add_rate(self) -> float:
        return float(self.added) / self.window_s

    @property
    def cancel_rate(self) -> float:
        return float(self.cancelled) / self.window_s

    def net_withdrawal_ratio(self, resting: Decimal) -> float:
        """(cancelled - added) / resting: >0 means liquidity pulled without trades."""
        base = resting if resting > 0 else Decimal(1)
        return float((self.cancelled - self.added) / base)

    @property
    def replenishment_ratio(self) -> float | None:
        """Share of executed liquidity that reappeared at the same prices (None: nothing traded)."""
        if self.executed == 0:
            return None
        return float(self.replenished / self.executed)


class LiquidityFlowTracker:
    """Tracks adds / cancels / executions / replenishment within ``band_bps`` of mid."""

    def __init__(self, band_bps: float, window_us: int, replenish_memory_us: int) -> None:
        self.band_bps = Decimal(str(band_bps))
        self.window_us = window_us
        self.replenish_memory_us = replenish_memory_us
        self._flows = {BookSide.BID: _SideFlow(window_us), BookSide.ASK: _SideFlow(window_us)}
        self._pending_exec: dict[tuple[BookSide, Decimal], Decimal] = {}
        self._consumed_at: dict[tuple[BookSide, Decimal], int] = {}

    def on_trade(self, trade: TradeEvent) -> None:
        # RPI orders are excluded from Bybit book messages (documented), so an RPI
        # fill cannot have consumed a visible level: never attribute it to one.
        if trade.raw.get("RPI") == "1":
            return
        # A buy aggressor lifts the ask; a sell aggressor hits the bid.
        side = BookSide.ASK if trade.side is Side.BUY else BookSide.BID
        key = (side, trade.price)
        self._pending_exec[key] = self._pending_exec.get(key, _ZERO) + trade.quantity

    def on_book_changes(self, now_us: int, changes: list[LevelChange], mid: Decimal | None) -> None:
        for flow in self._flows.values():
            flow.advance(now_us)
        if mid is not None:
            lo = mid * (1 - self.band_bps / _BPS)
            hi = mid * (1 + self.band_bps / _BPS)
            for ch in changes:
                if lo <= ch.price <= hi:
                    self._account(now_us, ch)
        self._pending_exec.clear()
        if len(self._consumed_at) > 4096:
            cutoff = now_us - self.replenish_memory_us
            self._consumed_at = {k: t for k, t in self._consumed_at.items() if t > cutoff}

    def _account(self, now_us: int, ch: LevelChange) -> None:
        flow = self._flows[ch.side]
        key = (ch.side, ch.price)
        delta = ch.change
        if delta < 0:
            removed = -delta
            executed = min(removed, self._pending_exec.get(key, _ZERO))
            cancelled = removed - executed
            if executed > 0:
                flow.executed.add(now_us, executed * ch.price)
                self._consumed_at[key] = now_us
                self._pending_exec[key] -= executed
            if cancelled > 0:
                flow.cancelled.add(now_us, cancelled * ch.price)
        elif delta > 0:
            flow.added.add(now_us, delta * ch.price)
            consumed_t = self._consumed_at.get(key)
            if consumed_t is not None and now_us - consumed_t <= self.replenish_memory_us:
                flow.replenished.add(now_us, delta * ch.price)

    def snapshot(self, now_us: int) -> dict[BookSide, SideLiquidityFlow]:
        out: dict[BookSide, SideLiquidityFlow] = {}
        for side, flow in self._flows.items():
            flow.advance(now_us)
            out[side] = SideLiquidityFlow(
                added=flow.added.total,
                cancelled=flow.cancelled.total,
                executed=flow.executed.total,
                replenished=flow.replenished.total,
                window_s=self.window_us / 1e6,
            )
        return out
