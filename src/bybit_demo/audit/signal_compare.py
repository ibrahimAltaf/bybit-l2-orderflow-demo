"""Compare CONFIRMED signals between replays by setup correspondence, not list position.

Setup IDs are assigned in order of appearance, so they differ between clocks. Two
signals correspond only if they have the same direction, confirm within
``match_window_s`` of each other, and each pairing is used once (closest first):

* SAME_SETUP      the two setups' [first state, CONFIRMED] intervals overlap
* CHANGED_SETUP   within the window, but the intervals do not overlap
* NOT_PRESENT     no corresponding signal; the reason lists what the setups in that
                  period did instead
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SAME_SETUP = "SAME_SETUP"
CHANGED_SETUP = "CHANGED_SETUP"
NOT_PRESENT = "NOT_PRESENT"


@dataclass(slots=True)
class Setup:
    direction: str
    setup_id: int
    start_us: int
    end_us: int
    states: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    entered_us: dict[str, int] = field(default_factory=dict)
    first_update_id: int | None = None
    confirm_update_id: int | None = None

    @property
    def signal_id(self) -> str:
        return f"{self.direction}-{self.setup_id}"

    @property
    def confirmed_us(self) -> int | None:
        return self.entered_us.get("CONFIRMED")

    @property
    def furthest_state(self) -> str:
        for state in ("CONFIRMED", "CANDIDATE", "WATCH"):
            if state in self.entered_us:
                return state
        return "IDLE"


def load_setups(log: Path) -> list[Setup]:
    """Every setup in a demo_events.jsonl, in order of first appearance."""
    setups: dict[tuple[str, int], Setup] = {}
    with log.open(encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            if rec.get("record_type") != "state_transition":
                continue
            key = (rec["direction"], rec["setup_id"])
            t = rec["ts_us"]
            s = setups.get(key)
            if s is None:
                s = setups[key] = Setup(key[0], key[1], t, t, first_update_id=rec["update_id"])
            new = rec["new_state"]
            # REJECTED -> IDLE / CONFIRMED -> IDLE only end a cooldown, not the setup's life.
            if new != "IDLE" or rec["previous_state"] == "WATCH":
                s.end_us = t
            s.states.append(new)
            s.entered_us.setdefault(new, t)
            s.flags.extend(rec.get("rejection_flags", []))
            if new == "CONFIRMED":
                s.confirm_update_id = rec["update_id"]
    return list(setups.values())


def _reason(base: Setup, others: list[Setup], pad_us: int) -> str:
    assert base.confirmed_us is not None
    lo, hi = base.start_us - pad_us, base.confirmed_us + pad_us
    near = [
        o for o in others if o.direction == base.direction and o.start_us <= hi and o.end_us >= lo
    ]
    if not near:
        return "no setup of this direction in the period"
    parts = []
    for o in near[:4]:
        flags = "+".join(dict.fromkeys(o.flags)) or "no rejection flag"
        parts.append(f"{o.signal_id} reached {o.furthest_state} ({flags})")
    return "; ".join(parts)


def compare_signals(
    baseline: list[Setup],
    corrected: list[Setup],
    *,
    match_window_s: float = 30.0,
    context_pad_s: float = 5.0,
) -> dict[str, Any]:
    window = int(match_window_s * 1_000_000)
    pad = int(context_pad_s * 1_000_000)
    base_sig = sorted(
        (s for s in baseline if s.confirmed_us is not None), key=lambda s: s.entered_us["CONFIRMED"]
    )
    corr_sig = [s for s in corrected if s.confirmed_us is not None]
    pairs = sorted(
        (abs(b.entered_us["CONFIRMED"] - c.entered_us["CONFIRMED"]), bi, ci)
        for bi, b in enumerate(base_sig)
        for ci, c in enumerate(corr_sig)
        if b.direction == c.direction
        and abs(b.entered_us["CONFIRMED"] - c.entered_us["CONFIRMED"]) <= window
    )
    match: dict[int, int] = {}
    used: set[int] = set()
    for _, bi, ci in pairs:
        if bi not in match and ci not in used:
            match[bi] = ci
            used.add(ci)
    rows: list[dict[str, Any]] = []
    for bi, b in enumerate(base_sig):
        row: dict[str, Any] = {
            "baseline_signal_id": b.signal_id,
            "direction": b.direction,
            "baseline_confirmed_us": b.entered_us["CONFIRMED"],
            "baseline_watch_us": b.entered_us.get("WATCH"),
            "baseline_candidate_us": b.entered_us.get("CANDIDATE"),
            "baseline_confirm_u": b.confirm_update_id,
        }
        paired = match.get(bi)
        if paired is None:
            row.update(
                correspondence=NOT_PRESENT,
                corrected_signal_id=None,
                corrected_confirmed_us=None,
                corrected_confirm_u=None,
                delta_ms=None,
                watch_delta_ms=None,
                candidate_delta_ms=None,
                reason=_reason(b, corrected, pad),
            )
        else:
            c = corr_sig[paired]
            overlap = (
                c.start_us <= b.entered_us["CONFIRMED"] and b.start_us <= c.entered_us["CONFIRMED"]
            )

            def delta(state: str, a: Setup = b, other: Setup = c) -> float | None:
                x, y = a.entered_us.get(state), other.entered_us.get(state)
                return None if x is None or y is None else (y - x) / 1000

            row.update(
                correspondence=SAME_SETUP if overlap else CHANGED_SETUP,
                corrected_signal_id=c.signal_id,
                corrected_confirmed_us=c.entered_us["CONFIRMED"],
                corrected_confirm_u=c.confirm_update_id,
                delta_ms=delta("CONFIRMED"),
                watch_delta_ms=delta("WATCH"),
                candidate_delta_ms=delta("CANDIDATE"),
                reason="" if overlap else "confirmed nearby from a different setup interval",
            )
        rows.append(row)
    new = [
        {
            "corrected_signal_id": s.signal_id,
            "direction": s.direction,
            "corrected_confirmed_us": s.entered_us["CONFIRMED"],
        }
        for i, s in enumerate(corr_sig)
        if i not in used
    ]
    counts = Counter(r["correspondence"] for r in rows)
    return {
        "rows": rows,
        "new_signals": new,
        "same_setup": counts[SAME_SETUP],
        "changed_setup": counts[CHANGED_SETUP],
        "not_present": counts[NOT_PRESENT],
        "corrected_confirmed": len(corr_sig),
        "new_count": len(new),
        "method": {
            "match_window_s": match_window_s,
            "context_pad_s": context_pad_s,
            "rule": "same direction, confirmed within the window, closest pairs first, one-to-one",
        },
    }
