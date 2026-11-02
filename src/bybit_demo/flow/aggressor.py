"""Aggressor-side resolution.

Bybit publishes the taker side (``S`` / CSV ``side``: "Side of taker"), so the
demo uses REPORTED sides. :class:`TickRuleClassifier` exists for sources that
do not publish it; any trade it classifies is labelled ``INFERRED`` and the
replay summary counts such trades separately.
"""

from __future__ import annotations

from decimal import Decimal

from bybit_demo.errors import SchemaError
from bybit_demo.models.events import Side

_SIDE_ALIASES: dict[str, Side] = {
    "buy": Side.BUY,
    "b": Side.BUY,
    "sell": Side.SELL,
    "s": Side.SELL,
}


def parse_reported_side(value: str) -> Side:
    """Map a provider's taker-side string to :class:`Side`; raise on anything else."""
    side = _SIDE_ALIASES.get(value.strip().lower())
    if side is None:
        raise SchemaError(f"unrecognised aggressor side {value!r}")
    return side


class TickRuleClassifier:
    """Classic tick rule: uptick -> buy, downtick -> sell, zero tick -> previous.

    Causal by construction: it only uses the current and previous trade price.
    Returns ``None`` until the first price change has been observed.
    """

    def __init__(self) -> None:
        self._last_price: Decimal | None = None
        self._last_side: Side | None = None

    def classify(self, price: Decimal) -> Side | None:
        side = self._last_side
        if self._last_price is not None:
            if price > self._last_price:
                side = Side.BUY
            elif price < self._last_price:
                side = Side.SELL
        self._last_price = price
        self._last_side = side
        return side
