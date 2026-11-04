"""Continuity classification on synthetic streams, including a legitimate u jump."""

from __future__ import annotations

from collections.abc import Sequence

from bybit_demo.book.continuity_audit import FindingClass as F
from bybit_demo.book.continuity_audit import audit_stream
from bybit_demo.models.events import BookEvent
from tests.helpers import delta, snap

BOOK = ([("10", "1")], [("11", "1")])


def counts(events: Sequence[BookEvent], **kw: object) -> dict[str, int]:
    return audit_stream(events, "TESTUSDT", **kw).counts()  # type: ignore[arg-type]


def test_contiguous_stream_is_not_called_complete() -> None:
    a = audit_stream([snap(1, *BOOK, ts=1), delta(2, ts=2), delta(3, ts=3)], "TESTUSDT")
    assert a.plus_one_increments == 2
    d = a.to_dict()
    assert "No claim of completeness" in d["completeness_statement"]


def test_missing_update_contradicted_by_snapshot_is_confirmed_missing() -> None:
    ev: list[BookEvent] = [
        snap(1, *BOOK, ts=1),
        delta(3, bids=[("10", "2")], ts=2),  # u=2 lost, and it carried the ask change
        snap(3, [("10", "2")], [("11", "7")], ts=3),
    ]
    c = counts(ev)
    assert c[F.CONFIRMED_MISSING.value] == 1
    assert c[F.OBSERVED_NONUNIT_U_INCREMENT.value] == 0


def test_legitimate_jump_confirmed_by_snapshot_is_only_observed() -> None:
    ev: list[BookEvent] = [
        snap(1, *BOOK, ts=1),
        delta(5, bids=[("10", "2")], ts=2),  # non-unit, but nothing was actually lost
        snap(5, [("10", "2")], [("11", "1")], ts=3),
    ]
    c = counts(ev)
    assert c[F.OBSERVED_NONUNIT_U_INCREMENT.value] == 1
    assert c[F.CONFIRMED_MISSING.value] == 0 and c[F.SUSPECTED_GAP.value] == 0


def test_jump_with_no_later_snapshot_stays_suspected() -> None:
    c = counts([snap(1, *BOOK, ts=1), delta(5, ts=2)])
    assert c[F.SUSPECTED_GAP.value] == 1 and c[F.OBSERVED_NONUNIT_U_INCREMENT.value] == 0


def test_duplicate_is_skipped_and_counted_not_classified_as_gap() -> None:
    a = audit_stream(
        [
            snap(1, *BOOK, ts=1),
            delta(2, bids=[("10", "2")], ts=2),
            delta(2, bids=[("10", "9")], ts=2),
        ],
        "TESTUSDT",
    )
    assert a.duplicates_skipped == 1 and a.counts()[F.SUSPECTED_GAP.value] == 0


def test_snapshot_and_restart_are_valid_boundaries_and_resynchronize() -> None:
    ev: list[BookEvent] = [
        snap(50, *BOOK, ts=1),
        delta(51, ts=2),
        snap(1, [("20", "1")], [("21", "1")], ts=3),  # service restart resets u
        delta(2, bids=[("20", "2")], ts=4),
    ]
    c = counts(ev)
    assert c[F.VALID_RESET_OR_SNAPSHOT_BOUNDARY.value] == 2
    assert c[F.SUSPECTED_GAP.value] == 0 and c[F.CONFIRMED_MISSING.value] == 0


def test_regressions_and_inconsistent_book_are_suspected() -> None:
    assert counts([snap(5, *BOOK, ts=1), delta(4, ts=2)])[F.SUSPECTED_GAP.value] == 1  # u back
    assert (
        counts([snap(1, *BOOK, ts=1), delta(2, ts=500), delta(3, ts=400)])[F.SUSPECTED_GAP.value]
        == 1
    )
    assert counts([snap(1, *BOOK, ts=1), delta(2, ts=2, seq=1)])[F.SUSPECTED_GAP.value] == 1
    assert (
        counts([snap(1, *BOOK, ts=1), delta(2, bids=[("9", "0")], ts=2)])[F.SUSPECTED_GAP.value]
        == 1
    )
    assert (
        counts([snap(1, *BOOK, ts=1), delta(2, bids=[("12", "1")], ts=2)])[F.SUSPECTED_GAP.value]
        == 1
    )
    assert counts([delta(2, ts=2)])[F.SUSPECTED_GAP.value] == 1  # no snapshot yet


def test_quiet_interval_and_missing_ids_are_unverifiable() -> None:
    quiet: list[BookEvent] = [snap(1, *BOOK, ts=1), delta(2, ts=100_000_000)]
    assert counts(quiet, quiet_threshold_s=30.0)[F.UNKNOWN_UNVERIFIABLE.value] == 1
    no_ids = [snap(1, *BOOK, ts=1)]
    assert counts(no_ids, has_update_ids=False)[F.UNKNOWN_UNVERIFIABLE.value] == 1
