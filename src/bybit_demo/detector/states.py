"""Mutable per-direction setup state (one instance per symbol x direction)."""

from __future__ import annotations

from dataclasses import dataclass

from bybit_demo.models.signals import Direction, SignalState


@dataclass(slots=True)
class SetupState:
    direction: Direction
    state: SignalState = SignalState.IDLE
    setup_id: int = 0
    entered_us: int = 0
    watch_start_us: int = 0
    watch_start_trades: int = 0
    last_core_ok_us: int = 0
    candidate_start_us: int = 0
    resume_at_us: int = 0  # cooldown end after CONFIRMED / REJECTED

    def enter(self, state: SignalState, now_us: int) -> None:
        self.state = state
        self.entered_us = now_us
