from __future__ import annotations

from bybit_demo.book.continuity import (
    BookSynchronizer,
    BybitContinuityValidator,
    IssueKind,
    TimestampOnlyContinuityValidator,
)
from bybit_demo.book.reconstruction import OrderBook
from tests.helpers import delta, snap

BIDS = [("100", "1"), ("99", "1")]
ASKS = [("101", "1"), ("102", "1")]


def make(strict: bool = True) -> BookSynchronizer:
    return BookSynchronizer(
        OrderBook("TESTUSDT"),
        BybitContinuityValidator("TESTUSDT", require_contiguous_update_id=strict),
    )


def kinds(sync: BookSynchronizer) -> list[IssueKind]:
    return [i.kind for i in sync.issues]


def test_contiguous_stream_has_no_failures() -> None:
    s = make()
    s.process(snap(10, BIDS, ASKS, ts=1))
    for u in range(11, 20):
        assert s.process(delta(u, bids=[("100", str(u))], ts=u)).applied
    assert s.stats.failures() == 0
    assert s.stats.deltas_applied == 9
    assert s.usable


def test_delta_before_snapshot_rejected() -> None:
    s = make()
    upd = s.process(delta(5, bids=[("1", "1")]))
    assert not upd.applied
    assert kinds(s) == [IssueKind.DELTA_BEFORE_SNAPSHOT]
    assert not s.usable


def test_duplicate_skipped() -> None:
    s = make()
    s.process(snap(10, BIDS, ASKS))
    s.process(delta(11, bids=[("100", "2")]))
    upd = s.process(delta(11, bids=[("100", "9")]))
    assert not upd.applied
    assert IssueKind.DUPLICATE in kinds(s)
    assert s.book.best_bid()[1] == 2  # type: ignore[index]


def test_stale_update_skipped() -> None:
    s = make()
    s.process(snap(10, BIDS, ASKS))
    s.process(delta(11))
    assert not s.process(delta(9)).applied
    assert IssueKind.STALE_UPDATE in kinds(s)


def test_sequence_gap_invalidates_until_resync() -> None:
    s = make()
    s.process(snap(10, BIDS, ASKS, ts=1))
    s.process(delta(11, ts=2))
    gap = s.process(delta(13, bids=[("100", "5")], ts=3))
    assert not gap.applied
    assert gap.issues[0].kind is IssueKind.SEQUENCE_GAP
    assert gap.issues[0].details["missing_updates"] == 1
    assert not s.usable
    # Subsequent deltas are refused, not silently applied to a broken book.
    assert not s.process(delta(14, ts=4)).applied
    assert IssueKind.DELTA_WHILE_UNSYNCED in kinds(s)
    # A fresh snapshot resynchronizes.
    s.process(snap(20, BIDS, ASKS, ts=5))
    assert IssueKind.RESYNC in kinds(s)
    assert s.usable
    assert s.process(delta(21, ts=6)).applied


def test_non_strict_mode_accepts_jumps() -> None:
    s = make(strict=False)
    s.process(snap(10, BIDS, ASKS))
    assert s.process(delta(15)).applied
    assert s.stats.failures() == 0


def test_seq_regression_and_timestamp_regression_warn_but_apply() -> None:
    s = make()
    s.process(snap(10, BIDS, ASKS, ts=100, seq=500))
    upd = s.process(delta(11, ts=50, seq=400))
    assert upd.applied
    assert {IssueKind.SEQ_REGRESSION, IssueKind.TIMESTAMP_REGRESSION} <= set(kinds(s))


def test_service_restart_snapshot_u1() -> None:
    s = make()
    s.process(snap(10, BIDS, ASKS))
    s.process(delta(11))
    s.process(snap(1, BIDS, ASKS))
    assert IssueKind.SERVICE_RESTART in kinds(s)
    assert s.process(delta(2)).applied


def test_crossed_book_flagged_once() -> None:
    s = make()
    s.process(snap(10, BIDS, ASKS))
    s.process(delta(11, bids=[("101.5", "1")]))
    s.process(delta(12, bids=[("101.6", "1")]))
    assert kinds(s).count(IssueKind.CROSSED_BOOK) == 1
    assert not s.usable
    s.process(delta(13, bids=[("101.5", "0"), ("101.6", "0")]))
    assert s.usable


def test_checkpoint_snapshot_match_and_mismatch() -> None:
    s = make()
    s.process(snap(10, BIDS, ASKS))
    s.process(delta(11, bids=[("100", "3")]))
    upd = s.process(snap(11, [("100", "3"), ("99", "1")], ASKS))
    assert upd.snapshot_comparison is not None and upd.snapshot_comparison.matches
    s.process(delta(12, bids=[("100", "4")]))
    upd = s.process(snap(12, [("100", "7"), ("99", "1")], ASKS))
    assert not upd.snapshot_comparison.matches  # type: ignore[union-attr]
    assert IssueKind.SNAPSHOT_MISMATCH in kinds(s)
    assert s.stats.checkpoint_comparisons == 2 and s.stats.checkpoint_matches == 1


def test_every_issue_is_recorded() -> None:
    s = make()
    s.process(delta(1))
    s.process(delta(2))
    assert len(s.issues) == 2
    assert s.stats.issue_counts == {"DELTA_BEFORE_SNAPSHOT": 2}


def test_timestamp_only_validator() -> None:
    s = BookSynchronizer(OrderBook("T"), TimestampOnlyContinuityValidator("T"))
    assert not s.process(delta(0, ts=1)).applied
    s.process(snap(0, BIDS, ASKS, ts=2))
    assert s.process(delta(0, bids=[("100", "2")], ts=3)).applied
    s.process(delta(0, ts=1))
    assert IssueKind.TIMESTAMP_REGRESSION in kinds(s)
