"""Value objects describing order-book state and changes."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class BookSide(StrEnum):
    BID = "bid"
    ASK = "ask"


class ChangeType(StrEnum):
    INSERT = "insert"
    INCREASE = "increase"
    DECREASE = "decrease"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class LevelChange:
    """The effect of a single delta entry on one price level."""

    side: BookSide
    price: Decimal
    old_quantity: Decimal
    new_quantity: Decimal

    @property
    def change(self) -> Decimal:
        return self.new_quantity - self.old_quantity

    @property
    def change_type(self) -> ChangeType:
        if self.old_quantity == 0:
            return ChangeType.INSERT
        if self.new_quantity == 0:
            return ChangeType.DELETE
        return ChangeType.INCREASE if self.new_quantity > self.old_quantity else ChangeType.DECREASE


@dataclass(frozen=True, slots=True)
class TopOfBook:
    best_bid: Decimal
    best_bid_qty: Decimal
    best_ask: Decimal
    best_ask_qty: Decimal

    @property
    def mid(self) -> Decimal:
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread(self) -> Decimal:
        return self.best_ask - self.best_bid

    @property
    def spread_bps(self) -> float:
        return float(self.spread / self.mid) * 1e4

    @property
    def microprice(self) -> Decimal:
        """Size-weighted mid (Stoikov-style first-order microprice).

        Leans toward the side with *less* resting size, i.e. the side more
        likely to be consumed next.
        """
        total = self.best_bid_qty + self.best_ask_qty
        if total == 0:
            return self.mid
        return (self.best_bid * self.best_ask_qty + self.best_ask * self.best_bid_qty) / total


@dataclass(frozen=True, slots=True)
class BandDepth:
    """Resting liquidity within ``bps`` of mid, in base quantity and USDT notional."""

    bps: float
    bid_qty: Decimal
    ask_qty: Decimal
    bid_notional: Decimal
    ask_notional: Decimal

    @property
    def imbalance(self) -> float:
        """(Bid - Ask) / (Bid + Ask) on notional; 0.0 when the band is empty."""
        total = self.bid_notional + self.ask_notional
        if total == 0:
            return 0.0
        return float((self.bid_notional - self.ask_notional) / total)
