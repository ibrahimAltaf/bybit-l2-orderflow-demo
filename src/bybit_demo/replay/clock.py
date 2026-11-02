"""Monotonic replay clock with optional wall-clock pacing."""

from __future__ import annotations

import time
from collections.abc import Callable

from bybit_demo.errors import ReplayOrderError


class ReplayClock:
    """Holds replay "now"; refuses to move backwards.

    ``speed == 0`` -> fast deterministic mode (no sleeping).
    ``speed > 0``  -> paced: event-time deltas are replayed at ``speed`` x real time.
    Pacing never changes event order or any computed value, only wall time.
    """

    def __init__(
        self,
        speed: float = 0.0,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if speed < 0:
            raise ValueError("speed must be >= 0")
        self.speed = speed
        self._sleep = sleep
        self._monotonic = monotonic
        self.now_us: int | None = None
        self._anchor: tuple[int, float] | None = None

    def advance(self, t_us: int) -> None:
        if self.now_us is not None and t_us < self.now_us:
            raise ReplayOrderError(f"replay clock cannot go backwards: {t_us} < {self.now_us}")
        self.now_us = t_us
        if self.speed <= 0:
            return
        if self._anchor is None:
            self._anchor = (t_us, self._monotonic())
            return
        t0, w0 = self._anchor
        target = w0 + (t_us - t0) / 1e6 / self.speed
        delay = target - self._monotonic()
        if delay > 0:
            self._sleep(delay)
