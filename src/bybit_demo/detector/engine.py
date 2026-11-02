"""WATCH -> CANDIDATE -> CONFIRMED state machine (one per direction).

Transitions::

    IDLE ──(core flow + structure, gates ok, not extended, not spike)──▶ WATCH
    IDLE ──(core flow + structure, but a gate / extension / spike fails)▶ REJECTED
    WATCH ──(persistence + candidate rules)─────────────────────────────▶ CANDIDATE
    WATCH ──(core flow absent > lapse_grace_ms)─────────────────────────▶ IDLE
    WATCH/CANDIDATE ──(gate or invalidation)────────────────────────────▶ REJECTED
    CANDIDATE ──(confirm rules, within extension cap)───────────────────▶ CONFIRMED
    CANDIDATE ──(max_duration_ms elapsed)───────────────────────────────▶ REJECTED
    CONFIRMED / REJECTED ──(cooldown elapsed)───────────────────────────▶ IDLE

Every transition yields a :class:`TransitionRecord` carrying the exact feature
values and thresholds evaluated at that instant.
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel

from bybit_demo.config import DetectorConfig
from bybit_demo.detector.invalidation import (
    extension_check,
    gate_failures,
    setup_invalidations,
    spike_check,
)
from bybit_demo.detector.rules import (
    DirectionalView,
    candidate_rules,
    confirm_rules,
    core_flow,
    watch_structure,
)
from bybit_demo.detector.states import SetupState
from bybit_demo.features.feature_engine import FeatureVector
from bybit_demo.models.signals import Direction, RejectionReason, SignalState, TransitionRecord

S = SignalState


def _flatten(prefix: str, model: BaseModel) -> dict[str, float | int]:
    out: dict[str, float | int] = {}
    for k, v in model.model_dump().items():
        if isinstance(v, bool):
            out[f"{prefix}.{k}"] = int(v)
        elif isinstance(v, int | float):
            out[f"{prefix}.{k}"] = v
    return out


class DetectorEngine:
    """Runs the LONG and SHORT machines for one symbol."""

    def __init__(
        self,
        symbol: str,
        cfg: DetectorConfig,
        directions: Iterable[Direction] = (Direction.LONG, Direction.SHORT),
    ) -> None:
        self.symbol = symbol
        self.cfg = cfg
        self._states = {d: SetupState(d) for d in directions}
        self._next_setup_id = 1
        g = _flatten("gates", cfg.gates) | {"gates.min_depth_usdt": cfg.gates.min_depth_usdt}
        inv = _flatten("invalidation", cfg.invalidation)
        self._thr = {
            S.IDLE: g | _flatten("watch", cfg.watch) | inv,
            S.WATCH: g | _flatten("watch", cfg.watch) | _flatten("candidate", cfg.candidate) | inv,
            S.CANDIDATE: g
            | _flatten("candidate", cfg.candidate)
            | _flatten("confirm", cfg.confirm)
            | inv,
            S.CONFIRMED: _flatten("cooldown", cfg.cooldown),
            S.REJECTED: _flatten("cooldown", cfg.cooldown),
        }

    def state(self, direction: Direction) -> SignalState:
        return self._states[direction].state

    def evaluate(self, fv: FeatureVector) -> list[TransitionRecord]:
        out: list[TransitionRecord] = []
        for st in self._states.values():
            rec = self._step(st, fv)
            if rec is not None:
                out.append(rec)
        return out

    # ----------------------------------------------------------------- core
    def _step(self, st: SetupState, fv: FeatureVector) -> TransitionRecord | None:
        now = fv.ts_us
        v = DirectionalView(fv, st.direction)
        c = self.cfg
        if st.state in (S.CONFIRMED, S.REJECTED):
            if now >= st.resume_at_us:
                return self._transition(st, fv, S.IDLE, ["cooldown_elapsed"], [])
            return None

        gates = gate_failures(fv, c.gates)
        core = core_flow(v, c.watch)

        if st.state is S.IDLE:
            if not core.ok:
                return None
            structure = watch_structure(v, c.watch)
            if not structure.ok:
                return None
            # Only a setup that would otherwise qualify for WATCH can be rejected.
            flags = (
                gates
                + extension_check(v, c.watch.max_extension_sigma)
                + spike_check(fv, c.invalidation)
            )
            st.setup_id = self._new_setup_id()
            if flags:
                return self._reject(st, fv, core.passed + structure.passed, flags)
            st.watch_start_us = st.last_core_ok_us = now
            st.watch_start_trades = fv.session_trades
            return self._transition(st, fv, S.WATCH, core.passed + structure.passed, [])

        # Active setup (WATCH or CANDIDATE): gates and invalidations first.
        flags = gates + setup_invalidations(v, c.invalidation)
        if st.state is S.WATCH:
            flags += extension_check(v, c.watch.max_extension_sigma)
        if flags:
            return self._reject(st, fv, [], flags)

        if st.state is S.WATCH:
            if core.ok:
                st.last_core_ok_us = now
            elif (now - st.last_core_ok_us) / 1000 > c.watch.lapse_grace_ms:
                return self._transition(st, fv, S.IDLE, ["watch_lapsed", *core.failed], [])
            cand = candidate_rules(
                v,
                c.candidate,
                c.watch,
                watch_duration_ms=(now - st.watch_start_us) / 1000,
                trades_in_watch=fv.session_trades - st.watch_start_trades,
            )
            if not cand.ok:
                return None
            spike = spike_check(fv, c.invalidation)
            if spike:
                return self._reject(st, fv, cand.passed, spike)
            st.candidate_start_us = now
            return self._transition(st, fv, S.CANDIDATE, [*cand.passed, "liquidity_gate_pass"], [])

        # CANDIDATE
        if (now - st.candidate_start_us) / 1000 > c.candidate.max_duration_ms:
            return self._reject(st, fv, [], [RejectionReason.CANDIDATE_TIMEOUT])
        conf = confirm_rules(v, c.confirm)
        if not conf.ok:
            return None
        late = extension_check(v, c.confirm.max_extension_sigma)
        if late:
            return self._reject(st, fv, conf.passed, late)
        rec = self._transition(
            st,
            fv,
            S.CONFIRMED,
            [*conf.passed, "execution_liquidity_ok", "within_extension_cap"],
            [],
        )
        st.resume_at_us = now + c.cooldown.after_confirm_ms * 1000
        return rec

    # -------------------------------------------------------------- helpers
    def _new_setup_id(self) -> int:
        sid = self._next_setup_id
        self._next_setup_id += 1
        return sid

    def _reject(
        self,
        st: SetupState,
        fv: FeatureVector,
        reasons: list[str],
        flags: list[RejectionReason],
    ) -> TransitionRecord:
        rec = self._transition(st, fv, S.REJECTED, reasons, flags)
        st.resume_at_us = fv.ts_us + self.cfg.cooldown.after_reject_ms * 1000
        return rec

    def _transition(
        self,
        st: SetupState,
        fv: FeatureVector,
        new: SignalState,
        reasons: list[str],
        flags: list[RejectionReason],
    ) -> TransitionRecord:
        prev = st.state
        rec = TransitionRecord(
            ts_us=fv.ts_us,
            symbol=self.symbol,
            direction=st.direction,
            previous_state=prev,
            new_state=new,
            reasons=tuple(reasons),
            rejection_flags=tuple(dict.fromkeys(flags)),
            features=fv.to_dict(),
            thresholds=self._thr[prev],
            event_index=fv.event_index,
            update_id=fv.update_id,
            cross_seq=fv.cross_seq,
            setup_id=st.setup_id,
        )
        st.enter(new, fv.ts_us)
        return rec
