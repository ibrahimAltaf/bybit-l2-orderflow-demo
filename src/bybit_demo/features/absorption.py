"""Absorption: large aggressive volume with unusually small price displacement.

*Bid absorption* (supports buyers): aggressive SELL notional over the window
is large relative to the symbol's own recent typical rate, yet the mid fell by
no more than ``max_displacement_bps``. *Ask absorption* is the mirror image.
Normalizing by the symbol's own long-window flow makes the feature
comparable across contracts of very different liquidity.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class AbsorptionReading:
    volume_vs_typical: float  # aggressive notional / typical notional for the same duration
    displacement_bps: float  # adverse move for the absorbing side (>= 0)
    absorbed: bool

    @property
    def score(self) -> float:
        return self.volume_vs_typical / (1.0 + self.displacement_bps)


def measure_absorption(
    aggressive_notional: Decimal,
    typical_notional: Decimal,
    adverse_move_bps: float,
    *,
    min_volume_vs_typical: float,
    max_displacement_bps: float,
) -> AbsorptionReading:
    """Pure function; ``typical_notional`` is per-side, scaled to the same window length."""
    ratio = 0.0 if typical_notional <= 0 else float(aggressive_notional / typical_notional)
    disp = max(0.0, adverse_move_bps)
    absorbed = ratio >= min_volume_vs_typical and disp <= max_displacement_bps
    return AbsorptionReading(ratio, disp, absorbed)
