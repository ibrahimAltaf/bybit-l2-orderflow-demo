"""Event-driven cumulative volume delta and rolling trade-flow metrics.

Updated on every trade -- there is no candle and no bar-close dependency.
All volume accumulators are exact ``Decimal``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from bybit_demo.flow.rolling import CountWindowSum, TimeWindowSum
from bybit_demo.models.events import AggressorSource, Side, TradeEvent

_ZERO = Decimal(0)


@dataclass(slots=True)
class CVDTracker:
    """Session-cumulative signed flow since replay start."""

    cvd_qty: Decimal = _ZERO
    cvd_notional: Decimal = _ZERO
    buy_qty: Decimal = _ZERO
    sell_qty: Decimal = _ZERO
    buy_notional: Decimal = _ZERO
    sell_notional: Decimal = _ZERO
    buy_count: int = 0
    sell_count: int = 0
    inferred_count: int = 0

    def on_trade(self, trade: TradeEvent) -> None:
        notional = trade.notional
        if trade.side is Side.BUY:
            self.buy_qty += trade.quantity
            self.buy_notional += notional
            self.buy_count += 1
            self.cvd_qty += trade.quantity
            self.cvd_notional += notional
        else:
            self.sell_qty += trade.quantity
            self.sell_notional += notional
            self.sell_count += 1
            self.cvd_qty -= trade.quantity
            self.cvd_notional -= notional
        if trade.aggressor_source is AggressorSource.INFERRED:
            self.inferred_count += 1

    @property
    def trade_count(self) -> int:
        return self.buy_count + self.sell_count


def _imbalance(a: Decimal, b: Decimal) -> float:
    total = a + b
    return 0.0 if total == 0 else float((a - b) / total)


class FlowWindow:
    """Aggressive buy/sell notional and trade counts over one time window."""

    __slots__ = ("buy", "buy_n", "sell", "sell_n", "window_us")

    def __init__(self, window_us: int) -> None:
        self.window_us = window_us
        self.buy = TimeWindowSum(window_us)
        self.sell = TimeWindowSum(window_us)
        self.buy_n = TimeWindowSum(window_us)
        self.sell_n = TimeWindowSum(window_us)

    def on_trade(self, trade: TradeEvent) -> None:
        t, n = trade.replay_ts_us, trade.notional
        is_buy = trade.side is Side.BUY
        (self.buy if is_buy else self.sell).add(t, n)
        (self.buy_n if is_buy else self.sell_n).add(t, Decimal(1))
        (self.sell if is_buy else self.buy).advance(t)
        (self.sell_n if is_buy else self.buy_n).advance(t)

    def advance(self, now_us: int) -> None:
        for w in (self.buy, self.sell, self.buy_n, self.sell_n):
            w.advance(now_us)

    @property
    def signed_notional(self) -> Decimal:
        """Rolling CVD in USDT over this window."""
        return self.buy.total - self.sell.total

    @property
    def total_notional(self) -> Decimal:
        return self.buy.total + self.sell.total

    @property
    def imbalance(self) -> float:
        """(BuyNotional - SellNotional) / (BuyNotional + SellNotional)."""
        return _imbalance(self.buy.total, self.sell.total)

    @property
    def count_imbalance(self) -> float:
        return _imbalance(self.buy_n.total, self.sell_n.total)

    @property
    def trade_count(self) -> int:
        return int(self.buy_n.total + self.sell_n.total)

    def largest_trade(self) -> Decimal:
        return max(self.buy.max_value(), self.sell.max_value())


class EventFlowWindow:
    """Signed notional over the last ``n`` trades (event-count window)."""

    __slots__ = ("gross", "n", "signed")

    def __init__(self, n: int) -> None:
        self.n = n
        self.signed = CountWindowSum(n)
        self.gross = CountWindowSum(n)

    def on_trade(self, trade: TradeEvent) -> None:
        notional = trade.notional
        self.signed.add(notional * trade.side.sign)
        self.gross.add(notional)

    @property
    def imbalance(self) -> float:
        g = self.gross.total
        return 0.0 if g == 0 else float(self.signed.total / g)
