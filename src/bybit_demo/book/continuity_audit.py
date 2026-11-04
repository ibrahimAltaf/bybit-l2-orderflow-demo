"""Source-aware continuity classification of a recorded book stream.

This does not assert completeness. An unbroken ``u`` sequence means only that no
break was observed: Bybit documents ``u`` as "Update ID" and states no increment rule,
so a non-unit increment is not by itself proof of loss, and +1 increments are not proof
of completeness. The archive has no heartbeat, so a loss inside a quiet interval
would leave no trace in timestamps.

Classes:

* CONFIRMED_MISSING   the rebuilt book contradicts a later snapshot: updates were lost
* SUSPECTED_GAP       internally inconsistent (u/seq/timestamp regression, delete of an
                      absent level, crossed book) or a non-unit increment that no
                      later snapshot could test
* OBSERVED_NONUNIT_U_INCREMENT   u jumped, and a later snapshot still matched the book
* VALID_RESET_OR_SNAPSHOT_BOUNDARY   a snapshot, including a u == 1 service restart
* UNKNOWN_UNVERIFIABLE   the source cannot answer: quiet intervals, or no update IDs
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from bybit_demo.book.reconstruction import OrderBook
from bybit_demo.models.events import BookDelta, BookEvent, BookSnapshot, us_to_iso
from bybit_demo.models.orderbook import BookSide


class FindingClass(StrEnum):
    CONFIRMED_MISSING = "CONFIRMED_MISSING"
    SUSPECTED_GAP = "SUSPECTED_GAP"
    OBSERVED_NONUNIT_U_INCREMENT = "OBSERVED_NONUNIT_U_INCREMENT"
    VALID_RESET_OR_SNAPSHOT_BOUNDARY = "VALID_RESET_OR_SNAPSHOT_BOUNDARY"
    UNKNOWN_UNVERIFIABLE = "UNKNOWN_UNVERIFIABLE"


@dataclass(frozen=True, slots=True)
class Finding:
    cls: FindingClass
    ts_us: int
    update_id: int | None
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "class": self.cls.value,
            "time": us_to_iso(self.ts_us),
            "update_id": self.update_id,
            "detail": self.detail,
        }


@dataclass(slots=True)
class ContinuityAudit:
    symbol: str
    quiet_threshold_us: int
    has_update_ids: bool
    records: int = 0
    duplicates_skipped: int = 0
    plus_one_increments: int = 0
    findings: list[Finding] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        c = Counter(f.cls.value for f in self.findings)
        return {k.value: c[k.value] for k in FindingClass}

    def to_dict(self, *, max_listed: int = 25) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "records": self.records,
            "has_update_ids": self.has_update_ids,
            "plus_one_increments": self.plus_one_increments,
            "duplicates_skipped": self.duplicates_skipped,
            "quiet_threshold_s": self.quiet_threshold_us / 1_000_000,
            "class_counts": self.counts(),
            "findings_listed": [f.to_dict() for f in self.findings[:max_listed]],
            "findings_total": len(self.findings),
            "completeness_statement": (
                "No claim of completeness. No contradicting evidence was found in what this "
                "source can show; losses inside quiet intervals are unobservable."
                if not self.counts()[FindingClass.CONFIRMED_MISSING.value]
                else "Updates are confirmed missing."
            ),
        }


def audit_stream(
    events: Iterable[BookEvent],
    symbol: str,
    *,
    has_update_ids: bool = True,
    quiet_threshold_s: float = 30.0,
) -> ContinuityAudit:
    """Classify one recorded stream. Pure: reads events, keeps its own book."""
    audit = ContinuityAudit(symbol, int(quiet_threshold_s * 1_000_000), has_update_ids)
    book = OrderBook(symbol)
    last_u: int | None = None
    last_seq: int | None = None
    last_t: int | None = None
    untested: list[Finding] = []  # non-unit increments awaiting a snapshot to test them
    crossed = False

    def note(cls: FindingClass, ev: BookEvent, detail: str) -> None:
        audit.findings.append(Finding(cls, ev.exchange_ts_us, ev.update_id, detail))

    for ev in events:
        audit.records += 1
        t = ev.exchange_ts_us
        if last_t is not None:
            if t < last_t:
                note(FindingClass.SUSPECTED_GAP, ev, f"timestamp regressed by {last_t - t} us")
            elif t - last_t > audit.quiet_threshold_us:
                note(
                    FindingClass.UNKNOWN_UNVERIFIABLE,
                    ev,
                    f"quiet interval of {(t - last_t) / 1_000_000:.1f} s; no heartbeat to test",
                )
        last_t = t
        if isinstance(ev, BookSnapshot):
            restart = ev.update_id == 1 and last_u is not None
            note(
                FindingClass.VALID_RESET_OR_SNAPSHOT_BOUNDARY,
                ev,
                "u == 1 service restart" if restart else "snapshot",
            )
            if book.is_initialized and not restart:
                cmp = book.compare_to_snapshot(ev)
                if cmp.matches:
                    audit.findings.extend(untested)
                else:
                    note(
                        FindingClass.CONFIRMED_MISSING,
                        ev,
                        f"rebuilt book differs from snapshot on "
                        f"{cmp.bid_mismatches + cmp.ask_mismatches} levels",
                    )
            elif untested:
                audit.findings.extend(_unresolved(untested))
            untested = []
            book.apply_snapshot(ev)
            crossed = False
        else:
            if not book.is_initialized:
                note(FindingClass.SUSPECTED_GAP, ev, "delta before any snapshot")
                continue
            u = ev.update_id
            if u is not None and last_u is not None:
                if u == last_u:
                    audit.duplicates_skipped += 1
                    continue
                if u < last_u:
                    note(FindingClass.SUSPECTED_GAP, ev, f"u went backwards from {last_u}")
                    continue
                if u == last_u + 1:
                    audit.plus_one_increments += 1
                else:
                    untested.append(
                        Finding(
                            FindingClass.OBSERVED_NONUNIT_U_INCREMENT,
                            t,
                            u,
                            f"u increased by {u - last_u}; a later snapshot matched",
                        )
                    )
            if ev.cross_seq is not None and last_seq is not None and ev.cross_seq <= last_seq:
                note(FindingClass.SUSPECTED_GAP, ev, "seq did not increase")
            absent = _absent_deletes(book, ev)
            if absent:
                note(FindingClass.SUSPECTED_GAP, ev, f"{absent} delete(s) of absent levels")
            book.apply_delta(ev)
            now_crossed = book.is_crossed()
            if now_crossed and not crossed:
                note(FindingClass.SUSPECTED_GAP, ev, "book crossed or locked")
            crossed = now_crossed
        last_u, last_seq = ev.update_id, ev.cross_seq
    audit.findings.extend(_unresolved(untested))
    if not has_update_ids and audit.records:
        audit.findings.append(
            Finding(
                FindingClass.UNKNOWN_UNVERIFIABLE,
                last_t or 0,
                None,
                "source has no update IDs: sequence continuity cannot be tested at all",
            )
        )
    return audit


def _unresolved(pending: list[Finding]) -> list[Finding]:
    return [
        Finding(
            FindingClass.SUSPECTED_GAP,
            f.ts_us,
            f.update_id,
            f.detail.replace("a later snapshot matched", "no later snapshot could test it"),
        )
        for f in pending
    ]


def _absent_deletes(book: OrderBook, ev: BookDelta) -> int:
    n = 0
    for side, updates in ((BookSide.BID, ev.bid_updates), (BookSide.ASK, ev.ask_updates)):
        n += sum(
            1 for lvl in updates if lvl.quantity == 0 and book.quantity_at(side, lvl.price) == 0
        )
    return n
