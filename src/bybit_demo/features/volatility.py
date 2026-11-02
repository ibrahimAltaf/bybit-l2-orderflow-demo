"""Event-time realized volatility, compression/expansion and extension.

Returns are log changes of the mid price, sampled whenever the mid changes
(event time, not clock bars).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from bybit_demo.flow.rolling import FloatTimeWindowSum, LaggedValue, TimeWindowExtrema

_US = 1_000_000


@dataclass(frozen=True, slots=True)
class VolatilitySnapshot:
    rv_short_bps: float
    rv_long_bps: float
    compression_ratio: (
        float | None
    )  # sqrt(var-rate short / var-rate long); None if no long-window variance
    ext_up_bps: float  # mid above the rolling minimum
    ext_down_bps: float  # mid below the rolling maximum
    ext_up_sigma: float
    ext_down_sigma: float
    sigma_ext_bps: float  # normalizer: expected move over the extension window


class VolatilityTracker:
    def __init__(
        self, short_s: float, long_s: float, extension_window_s: float, min_sigma_bps: float
    ) -> None:
        self.short_s, self.long_s = short_s, long_s
        self.extension_window_s = extension_window_s
        self.min_sigma_bps = min_sigma_bps
        self._short = FloatTimeWindowSum(int(short_s * _US))
        self._long = FloatTimeWindowSum(int(long_s * _US))
        self._extrema = TimeWindowExtrema(int(extension_window_s * _US))
        # The mid is a step function: the value in force at the window start
        # belongs to the window even if it was set before it.
        self._at_window_start = LaggedValue(int(extension_window_s * _US))
        self._last_mid: Decimal | None = None

    def on_mid(self, t_us: int, mid: Decimal) -> None:
        if self._last_mid is not None and mid != self._last_mid:
            r = math.log(float(mid / self._last_mid))
            self._short.add(t_us, r * r)
            self._long.add(t_us, r * r)
        if mid != self._last_mid:
            self._extrema.add(t_us, mid)
            self._at_window_start.add(t_us, mid)
        self._last_mid = mid

    def snapshot(self, now_us: int, mid: Decimal) -> VolatilitySnapshot:
        self._short.advance(now_us)
        self._long.advance(now_us)
        self._extrema.advance(now_us)
        rv_s = math.sqrt(max(self._short.total, 0.0)) * 1e4
        rv_l = math.sqrt(max(self._long.total, 0.0)) * 1e4
        rate_l = self._long.total / self.long_s
        ratio = (
            None if rate_l <= 0 else math.sqrt(max(self._short.total, 0.0) / self.short_s / rate_l)
        )
        start = self._at_window_start.value_at_lag(now_us)
        candidates = [
            v for v in (self._extrema.minimum, self._extrema.maximum, start) if v is not None
        ]
        lo = min([mid, *candidates])
        hi = max([mid, *candidates])
        up = float((mid - lo) / lo) * 1e4
        down = float((hi - mid) / hi) * 1e4
        sigma = max(rv_l * math.sqrt(self.extension_window_s / self.long_s), self.min_sigma_bps)
        return VolatilitySnapshot(rv_s, rv_l, ratio, up, down, up / sigma, down / sigma, sigma)
