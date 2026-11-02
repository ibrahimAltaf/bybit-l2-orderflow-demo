"""Provider-specific continuity validation and the synchronized book pipeline.

Bybit V5 ``orderbook.{depth}.{symbol}`` semantics used here
(https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook):

* ``type`` is ``snapshot`` or ``delta``; a new snapshot resets the local book.
* ``u`` is the update ID. ``u == 1`` on a snapshot signals a service restart.
* ``seq`` is the cross sequence; smaller ``seq`` means generated earlier.
* Quantity ``0`` deletes a level; unknown price inserts; known price updates.

Three different things must not be confused:

* protocol-documented: snapshot resets the book; ``u == 1`` means restart;
  smaller ``seq`` = earlier; quantity 0 deletes.
* empirically observed (verify-data, 1,265,452 archive deltas): ``u`` grew by
  exactly 1 on every delta. Bybit does NOT document this.
* our validation rule: with ``require_contiguous_update_id`` (default on), any
  increment other than +1 is treated as SEQUENCE_GAP and the book is
  invalidated until the next snapshot. If the live stream legitimately skips
  IDs, this rule is too strict (fails safe: blocks signals), never too lax.
  With the rule off, only duplicates / regressions are caught and real gaps
  would go undetected.

Tardis.dev CSV ``incremental_book_L2`` rows carry no update ID, so only
timestamp monotonicity and snapshot handling can be checked for that source.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from bybit_demo.book.reconstruction import OrderBook, SnapshotComparison
from bybit_demo.models.events import BookDelta, BookEvent, BookSnapshot, us_to_iso
from bybit_demo.models.orderbook import LevelChange


class IssueKind(StrEnum):
    SNAPSHOT_INIT = "SNAPSHOT_INIT"
    SNAPSHOT_RESET = "SNAPSHOT_RESET"
    SERVICE_RESTART = "SERVICE_RESTART"
    RESYNC = "RESYNC"
    DELTA_BEFORE_SNAPSHOT = "DELTA_BEFORE_SNAPSHOT"
    DELTA_WHILE_UNSYNCED = "DELTA_WHILE_UNSYNCED"
    DUPLICATE = "DUPLICATE"
    STALE_UPDATE = "STALE_UPDATE"
    SEQUENCE_GAP = "SEQUENCE_GAP"
    SEQ_REGRESSION = "SEQ_REGRESSION"
    TIMESTAMP_REGRESSION = "TIMESTAMP_REGRESSION"
    CROSSED_BOOK = "CROSSED_BOOK"
    SNAPSHOT_MISMATCH = "SNAPSHOT_MISMATCH"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


_SEVERITY: dict[IssueKind, Severity] = {
    IssueKind.SNAPSHOT_INIT: Severity.INFO,
    IssueKind.SNAPSHOT_RESET: Severity.INFO,
    IssueKind.SERVICE_RESTART: Severity.WARNING,
    IssueKind.RESYNC: Severity.WARNING,
    IssueKind.DELTA_BEFORE_SNAPSHOT: Severity.ERROR,
    IssueKind.DELTA_WHILE_UNSYNCED: Severity.ERROR,
    IssueKind.DUPLICATE: Severity.WARNING,
    IssueKind.STALE_UPDATE: Severity.ERROR,
    IssueKind.SEQUENCE_GAP: Severity.ERROR,
    IssueKind.SEQ_REGRESSION: Severity.WARNING,
    IssueKind.TIMESTAMP_REGRESSION: Severity.WARNING,
    IssueKind.CROSSED_BOOK: Severity.ERROR,
    IssueKind.SNAPSHOT_MISMATCH: Severity.ERROR,
}

# Issues that are part of normal operation rather than continuity failures.
_BENIGN = frozenset({IssueKind.SNAPSHOT_INIT, IssueKind.SNAPSHOT_RESET})


@dataclass(frozen=True, slots=True)
class ContinuityIssue:
    kind: IssueKind
    symbol: str
    ts_us: int
    update_id: int | None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def severity(self) -> Severity:
        return _SEVERITY[self.kind]

    @property
    def is_failure(self) -> bool:
        return self.kind not in _BENIGN

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": "continuity_issue",
            "kind": self.kind.value,
            "severity": self.severity.value,
            "symbol": self.symbol,
            "timestamp": us_to_iso(self.ts_us),
            "ts_us": self.ts_us,
            "update_id": self.update_id,
            "details": self.details,
        }


@dataclass(frozen=True, slots=True)
class ContinuityDecision:
    apply: bool
    issues: tuple[ContinuityIssue, ...] = ()
    invalidate_book: bool = False


class ContinuityValidator(Protocol):
    """Decides whether a book event may be applied, per provider semantics."""

    @property
    def synced(self) -> bool: ...

    def check(self, event: BookEvent) -> ContinuityDecision: ...

    def mark_unsynced(self) -> None: ...


class BybitContinuityValidator:
    """``u`` / ``seq`` / ``ts`` rules for Bybit V5 orderbook streams and archives."""

    def __init__(self, symbol: str, *, require_contiguous_update_id: bool = True) -> None:
        self.symbol = symbol
        self.require_contiguous = require_contiguous_update_id
        self._synced = False
        self._ever_synced = False
        self._last_u: int | None = None
        self._last_seq: int | None = None
        self._last_ts: int | None = None

    @property
    def synced(self) -> bool:
        return self._synced

    def mark_unsynced(self) -> None:
        self._synced = False

    def _issue(self, kind: IssueKind, ev: BookEvent, **details: Any) -> ContinuityIssue:
        return ContinuityIssue(kind, self.symbol, ev.exchange_ts_us, ev.update_id, details)

    def check(self, event: BookEvent) -> ContinuityDecision:
        if isinstance(event, BookSnapshot):
            return self._check_snapshot(event)
        return self._check_delta(event)

    def _check_snapshot(self, ev: BookSnapshot) -> ContinuityDecision:
        issues: list[ContinuityIssue] = []
        if ev.update_id == 1 and self._last_u is not None:
            issues.append(self._issue(IssueKind.SERVICE_RESTART, ev, previous_u=self._last_u))
        if not self._ever_synced:
            issues.append(self._issue(IssueKind.SNAPSHOT_INIT, ev))
        elif not self._synced:
            issues.append(self._issue(IssueKind.RESYNC, ev, previous_u=self._last_u))
        else:
            issues.append(self._issue(IssueKind.SNAPSHOT_RESET, ev, previous_u=self._last_u))
        issues.extend(self._timestamp_check(ev))
        self._synced = self._ever_synced = True
        self._remember(ev)
        return ContinuityDecision(apply=True, issues=tuple(issues))

    def _check_delta(self, ev: BookDelta) -> ContinuityDecision:
        if not self._synced:
            kind = (
                IssueKind.DELTA_WHILE_UNSYNCED
                if self._ever_synced
                else IssueKind.DELTA_BEFORE_SNAPSHOT
            )
            return ContinuityDecision(apply=False, issues=(self._issue(kind, ev),))
        u, last_u = ev.update_id, self._last_u
        if u is None or last_u is None:
            # Bybit always publishes u; a missing value is a schema defect upstream.
            return ContinuityDecision(
                apply=False, issues=(self._issue(IssueKind.STALE_UPDATE, ev, reason="missing u"),)
            )
        if u == last_u:
            return ContinuityDecision(apply=False, issues=(self._issue(IssueKind.DUPLICATE, ev),))
        if u < last_u:
            return ContinuityDecision(
                apply=False, issues=(self._issue(IssueKind.STALE_UPDATE, ev, last_u=last_u),)
            )
        if self.require_contiguous and u != last_u + 1:
            self._synced = False
            issue = self._issue(
                IssueKind.SEQUENCE_GAP,
                ev,
                last_u=last_u,
                missing_updates=u - last_u - 1,
            )
            return ContinuityDecision(apply=False, issues=(issue,), invalidate_book=True)
        issues: list[ContinuityIssue] = []
        if (
            ev.cross_seq is not None
            and self._last_seq is not None
            and ev.cross_seq <= self._last_seq
        ):
            issues.append(
                self._issue(IssueKind.SEQ_REGRESSION, ev, seq=ev.cross_seq, last_seq=self._last_seq)
            )
        issues.extend(self._timestamp_check(ev))
        self._remember(ev)
        return ContinuityDecision(apply=True, issues=tuple(issues))

    def _timestamp_check(self, ev: BookEvent) -> list[ContinuityIssue]:
        if self._last_ts is not None and ev.exchange_ts_us < self._last_ts:
            return [self._issue(IssueKind.TIMESTAMP_REGRESSION, ev, last_ts_us=self._last_ts)]
        return []

    def _remember(self, ev: BookEvent) -> None:
        self._last_u = ev.update_id
        self._last_seq = ev.cross_seq
        self._last_ts = ev.exchange_ts_us


class TimestampOnlyContinuityValidator:
    """For sources without update IDs (Tardis.dev CSV): snapshot + ts checks only.

    Sequence gaps are undetectable for such sources; this is reported as a
    limitation, never silently treated as "no gaps".
    """

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self._synced = False
        self._ever_synced = False
        self._last_ts: int | None = None

    @property
    def synced(self) -> bool:
        return self._synced

    def mark_unsynced(self) -> None:
        self._synced = False

    def check(self, event: BookEvent) -> ContinuityDecision:
        issues: list[ContinuityIssue] = []
        ts = event.exchange_ts_us
        if self._last_ts is not None and ts < self._last_ts:
            issues.append(
                ContinuityIssue(
                    IssueKind.TIMESTAMP_REGRESSION,
                    self.symbol,
                    ts,
                    None,
                    {"last_ts_us": self._last_ts},
                )
            )
        if isinstance(event, BookSnapshot):
            kind = IssueKind.SNAPSHOT_RESET if self._ever_synced else IssueKind.SNAPSHOT_INIT
            issues.append(ContinuityIssue(kind, self.symbol, ts, None))
            self._synced = self._ever_synced = True
            self._last_ts = ts
            return ContinuityDecision(apply=True, issues=tuple(issues))
        if not self._synced:
            issues.append(ContinuityIssue(IssueKind.DELTA_BEFORE_SNAPSHOT, self.symbol, ts, None))
            return ContinuityDecision(apply=False, issues=tuple(issues))
        self._last_ts = ts
        return ContinuityDecision(apply=True, issues=tuple(issues))


@dataclass(slots=True)
class BookUpdate:
    """Outcome of feeding one book event through the synchronizer."""

    applied: bool
    changes: list[LevelChange]
    issues: tuple[ContinuityIssue, ...]
    snapshot_comparison: SnapshotComparison | None = None
    is_snapshot: bool = False


@dataclass(slots=True)
class ContinuityStats:
    snapshots: int = 0
    deltas_applied: int = 0
    deltas_skipped: int = 0
    checkpoint_comparisons: int = 0
    checkpoint_matches: int = 0
    issue_counts: dict[str, int] = field(default_factory=dict)

    def failures(self) -> int:
        benign = {IssueKind.SNAPSHOT_INIT.value, IssueKind.SNAPSHOT_RESET.value}
        return sum(n for k, n in self.issue_counts.items() if k not in benign)


class BookSynchronizer:
    """Validator + book + issue log for one symbol.

    Every continuity issue is appended to :attr:`issues` (nothing is dropped).
    When a snapshot arrives while the book is in sync, the reconstructed book
    is compared level-by-level against it *before* the reset: this is an
    independent correctness check of the incremental reconstruction.
    """

    def __init__(self, book: OrderBook, validator: ContinuityValidator) -> None:
        self.book = book
        self.validator = validator
        self.issues: list[ContinuityIssue] = []
        self.stats = ContinuityStats()
        self.crossed = False

    def process(self, event: BookEvent) -> BookUpdate:
        was_synced = self.validator.synced and self.book.is_initialized
        decision = self.validator.check(event)
        issues = list(decision.issues)
        comparison: SnapshotComparison | None = None
        changes: list[LevelChange] = []

        if decision.invalidate_book:
            self.book.invalidate()

        if decision.apply and isinstance(event, BookSnapshot):
            if was_synced:
                comparison = self.book.compare_to_snapshot(event)
                self.stats.checkpoint_comparisons += 1
                if comparison.matches:
                    self.stats.checkpoint_matches += 1
                else:
                    issues.append(
                        ContinuityIssue(
                            IssueKind.SNAPSHOT_MISMATCH,
                            self.book.symbol,
                            event.exchange_ts_us,
                            event.update_id,
                            {
                                "bid_mismatches": comparison.bid_mismatches,
                                "ask_mismatches": comparison.ask_mismatches,
                            },
                        )
                    )
            self.book.apply_snapshot(event)
            self.stats.snapshots += 1
        elif decision.apply and isinstance(event, BookDelta):
            changes = self.book.apply_delta(event)
            self.stats.deltas_applied += 1
        else:
            self.stats.deltas_skipped += 1

        crossed_now = decision.apply and self.book.is_crossed()
        if crossed_now and not self.crossed:
            top = self.book.top()
            issues.append(
                ContinuityIssue(
                    IssueKind.CROSSED_BOOK,
                    self.book.symbol,
                    event.exchange_ts_us,
                    event.update_id,
                    {"best_bid": str(top.best_bid), "best_ask": str(top.best_ask)} if top else {},
                )
            )
        if decision.apply:
            self.crossed = crossed_now

        for issue in issues:
            self.stats.issue_counts[issue.kind.value] = (
                self.stats.issue_counts.get(issue.kind.value, 0) + 1
            )
        self.issues.extend(issues)
        return BookUpdate(
            applied=decision.apply,
            changes=changes,
            issues=tuple(issues),
            snapshot_comparison=comparison,
            is_snapshot=isinstance(event, BookSnapshot),
        )

    @property
    def usable(self) -> bool:
        """True when the book is synced, initialized and not crossed."""
        return self.validator.synced and self.book.is_initialized and not self.crossed
