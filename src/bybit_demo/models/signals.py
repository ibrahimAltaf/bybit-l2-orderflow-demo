"""Signal-state and audit-record models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from bybit_demo.models.events import us_to_iso


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def sign(self) -> int:
        return 1 if self is Direction.LONG else -1


class SignalState(StrEnum):
    IDLE = "IDLE"
    WATCH = "WATCH"
    CANDIDATE = "CANDIDATE"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"


class RejectionReason(StrEnum):
    """Explicit, enumerable reasons a setup is rejected."""

    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    INSUFFICIENT_DEPTH = "INSUFFICIENT_DEPTH"
    ALREADY_EXTENDED = "ALREADY_EXTENDED"
    FLOW_REVERSAL = "FLOW_REVERSAL"
    CVD_DIVERGENCE = "CVD_DIVERGENCE"
    BOOK_PRESSURE_REVERSAL = "BOOK_PRESSURE_REVERSAL"
    SEQUENCE_GAP = "SEQUENCE_GAP"
    STALE_BOOK = "STALE_BOOK"
    CROSSED_BOOK = "CROSSED_BOOK"
    ISOLATED_SPIKE = "ISOLATED_SPIKE"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    CANDIDATE_TIMEOUT = "CANDIDATE_TIMEOUT"


@dataclass(frozen=True, slots=True)
class TransitionRecord:
    """One auditable state transition.

    ``features`` and ``thresholds`` hold the exact values the rules saw at
    ``ts_us`` -- nothing computed from later events.
    """

    ts_us: int
    symbol: str
    direction: Direction
    previous_state: SignalState
    new_state: SignalState
    reasons: tuple[str, ...]
    rejection_flags: tuple[RejectionReason, ...]
    features: dict[str, float | int | str | None]
    thresholds: dict[str, float | int]
    event_index: int
    update_id: int | None
    cross_seq: int | None
    setup_id: int
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_signal(self) -> bool:
        return self.new_state is SignalState.CONFIRMED

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": "state_transition",
            "timestamp": us_to_iso(self.ts_us),
            "ts_us": self.ts_us,
            "symbol": self.symbol,
            "direction": self.direction.value,
            "setup_id": self.setup_id,
            "previous_state": self.previous_state.value,
            "new_state": self.new_state.value,
            "signal_timestamp": us_to_iso(self.ts_us) if self.is_signal else None,
            "reasons": list(self.reasons),
            "rejection_flags": [r.value for r in self.rejection_flags],
            "event_index": self.event_index,
            "update_id": self.update_id,
            "cross_seq": self.cross_seq,
            "features": self.features,
            "thresholds": self.thresholds,
            **({"extra": self.extra} if self.extra else {}),
        }
