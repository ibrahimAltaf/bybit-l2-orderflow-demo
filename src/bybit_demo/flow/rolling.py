"""Causal rolling-window primitives.

Every window only ever receives values at the current replay time and evicts
relative to the *current* time it is told about. None of them can observe a
value that has not yet been pushed, so they cannot leak future information.
Pushing a timestamp older than the last one raises.
"""

from __future__ import annotations

import math
from collections import deque
from decimal import Decimal

from bybit_demo.errors import ReplayOrderError

_ZERO = Decimal(0)


class TimeWindowSum:
    """Exact (Decimal) sum of values with timestamps in ``(now - window, now]``."""

    __slots__ = ("_items", "_last_t", "count", "total", "window_us")

    def __init__(self, window_us: int) -> None:
        if window_us <= 0:
            raise ValueError("window_us must be positive")
        self.window_us = window_us
        self._items: deque[tuple[int, Decimal]] = deque()
        self.total = _ZERO
        self.count = 0
        self._last_t: int | None = None

    def add(self, t_us: int, value: Decimal) -> None:
        self._check(t_us)
        self._items.append((t_us, value))
        self.total += value
        self.count += 1
        self.advance(t_us)

    def advance(self, now_us: int) -> None:
        self._check(now_us)
        cutoff = now_us - self.window_us
        items = self._items
        while items and items[0][0] <= cutoff:
            _, v = items.popleft()
            self.total -= v
            self.count -= 1

    def max_value(self) -> Decimal:
        return max((v for _, v in self._items), default=_ZERO)

    def oldest_t(self) -> int | None:
        return self._items[0][0] if self._items else None

    def _check(self, t_us: int) -> None:
        if self._last_t is not None and t_us < self._last_t:
            raise ReplayOrderError(f"time moved backwards: {t_us} < {self._last_t}")
        self._last_t = t_us


class FloatTimeWindowSum:
    """Float sum over a time window, periodically re-summed with ``math.fsum``.

    The periodic exact re-summation bounds floating-point drift from repeated
    add/subtract; the result is deterministic for a given input sequence.
    """

    __slots__ = ("_evictions", "_items", "_last_t", "total", "window_us")
    _RESUM_EVERY = 1024

    def __init__(self, window_us: int) -> None:
        if window_us <= 0:
            raise ValueError("window_us must be positive")
        self.window_us = window_us
        self._items: deque[tuple[int, float]] = deque()
        self.total = 0.0
        self._evictions = 0
        self._last_t: int | None = None

    def __len__(self) -> int:
        return len(self._items)

    def add(self, t_us: int, value: float) -> None:
        self._check(t_us)
        self._items.append((t_us, value))
        self.total += value
        self.advance(t_us)

    def advance(self, now_us: int) -> None:
        self._check(now_us)
        cutoff = now_us - self.window_us
        items = self._items
        while items and items[0][0] <= cutoff:
            _, v = items.popleft()
            self.total -= v
            self._evictions += 1
        if self._evictions >= self._RESUM_EVERY:
            self.total = math.fsum(v for _, v in items)
            self._evictions = 0
        if not items:
            self.total = 0.0

    def _check(self, t_us: int) -> None:
        if self._last_t is not None and t_us < self._last_t:
            raise ReplayOrderError(f"time moved backwards: {t_us} < {self._last_t}")
        self._last_t = t_us


class TimeWindowExtrema:
    """Rolling min and max over ``(now - window, now]`` via monotonic deques."""

    __slots__ = ("_last_t", "_max", "_min", "window_us")

    def __init__(self, window_us: int) -> None:
        self.window_us = window_us
        self._min: deque[tuple[int, Decimal]] = deque()
        self._max: deque[tuple[int, Decimal]] = deque()
        self._last_t: int | None = None

    def add(self, t_us: int, value: Decimal) -> None:
        if self._last_t is not None and t_us < self._last_t:
            raise ReplayOrderError(f"time moved backwards: {t_us} < {self._last_t}")
        self._last_t = t_us
        while self._min and self._min[-1][1] >= value:
            self._min.pop()
        self._min.append((t_us, value))
        while self._max and self._max[-1][1] <= value:
            self._max.pop()
        self._max.append((t_us, value))
        self.advance(t_us)

    def advance(self, now_us: int) -> None:
        cutoff = now_us - self.window_us
        while self._min and self._min[0][0] <= cutoff:
            self._min.popleft()
        while self._max and self._max[0][0] <= cutoff:
            self._max.popleft()

    @property
    def minimum(self) -> Decimal | None:
        return self._min[0][1] if self._min else None

    @property
    def maximum(self) -> Decimal | None:
        return self._max[0][1] if self._max else None


class LaggedValue:
    """The most recent value observed at or before ``now - lag``.

    Used for "mid price ``lag`` ago" style displacement features.
    """

    __slots__ = ("_items", "lag_us")

    def __init__(self, lag_us: int) -> None:
        self.lag_us = lag_us
        self._items: deque[tuple[int, Decimal]] = deque()

    def add(self, t_us: int, value: Decimal) -> None:
        if self._items and t_us < self._items[-1][0]:
            raise ReplayOrderError(f"time moved backwards: {t_us} < {self._items[-1][0]}")
        self._items.append((t_us, value))

    def value_at_lag(self, now_us: int) -> Decimal | None:
        cutoff = now_us - self.lag_us
        items = self._items
        # Keep exactly one sample at/before the cutoff (the answer); drop older ones.
        while len(items) >= 2 and items[1][0] <= cutoff:
            items.popleft()
        if items and items[0][0] <= cutoff:
            return items[0][1]
        return None


class CountWindowSum:
    """Exact sum over the last ``n`` pushed values (event-count window)."""

    __slots__ = ("_items", "n", "total")

    def __init__(self, n: int) -> None:
        if n <= 0:
            raise ValueError("n must be positive")
        self.n = n
        self._items: deque[Decimal] = deque()
        self.total = _ZERO

    def __len__(self) -> int:
        return len(self._items)

    def add(self, value: Decimal) -> None:
        self._items.append(value)
        self.total += value
        if len(self._items) > self.n:
            self.total -= self._items.popleft()

    @property
    def full(self) -> bool:
        return len(self._items) == self.n
