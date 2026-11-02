"""Book-pressure, spread-regime and depth-adequacy features."""

from __future__ import annotations

from enum import StrEnum

from bybit_demo.models.orderbook import BandDepth


class SpreadRegime(StrEnum):
    TIGHT = "TIGHT"
    NORMAL = "NORMAL"
    WIDE = "WIDE"


def book_pressure(bands: tuple[BandDepth, ...], weights: tuple[float, ...]) -> float:
    """Weighted average of per-band (Bid-Ask)/(Bid+Ask) imbalances, in [-1, 1]."""
    total_w = sum(weights)
    if total_w <= 0:
        return 0.0
    return sum(b.imbalance * w for b, w in zip(bands, weights, strict=True)) / total_w


def spread_regime(spread_bps: float, tight_bps: float, max_bps: float) -> SpreadRegime:
    if spread_bps <= tight_bps:
        return SpreadRegime.TIGHT
    if spread_bps <= max_bps:
        return SpreadRegime.NORMAL
    return SpreadRegime.WIDE


def min_side_depth(band: BandDepth) -> float:
    """Depth adequacy: the thinner side's notional within the band (USDT)."""
    return float(min(band.bid_notional, band.ask_notional))
