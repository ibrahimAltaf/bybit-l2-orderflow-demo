# Order-Book Continuity Methodology

## What is and is not claimed

Bybit documents `u` as "Update ID", states that `u` = 1 on a snapshot means a service
restart, and states that a smaller `seq` was generated earlier. **It gives no increment
rule for `u`.** Earlier wording such as "zero gaps" is withdrawn. The statement supported
by evidence is: *no `u` increment other than +1 was observed in the three inspected files.*
That is not a guarantee, and it is not proof that no event is missing.

## Classes (`src/bybit_demo/book/continuity_audit.py`)

| Class | Assigned when |
|---|---|
| CONFIRMED_MISSING | The rebuilt book contradicts a later snapshot |
| SUSPECTED_GAP | `u`, `seq` or timestamp regression; delete of an absent level; crossed or locked book; delta before a snapshot; a non-unit increment with no later snapshot to test it |
| OBSERVED_NONUNIT_U_INCREMENT | `u` jumped, and a later snapshot still matched |
| VALID_RESET_OR_SNAPSHOT_BOUNDARY | Any snapshot, including a `u` = 1 restart |
| UNKNOWN_UNVERIFIABLE | Quiet interval over the threshold, or a source without update IDs |

Exact duplicates of `u` are skipped and counted separately.

## Results (`python scripts/verify_continuity.py`)

| Dataset | Records | CONFIRMED_MISSING | OBSERVED_NONUNIT_U_INCREMENT | SUSPECTED_GAP | UNKNOWN_UNVERIFIABLE | VALID_RESET_OR_SNAPSHOT_BOUNDARY |
|---|---|---|---|---|---|---|
| bybit_ORDIUSDT_2026-10-01 | 339,156 | 0 | 0 | 0 | 0 | 2 |
| bybit_10000LADYSUSDT_2025-10-01 | 553,443 | 0 | 0 | 0 | 0 | 2 |
| bybit_10000LADYSUSDT_2025-06-01 | 372,859 | 0 | 0 | 0 | 0 | 2 |
| tardis_ORDIUSDT_2026-10-01 | 651,959 | 0 | 0 | 0 | 1 | 2 |

Quiet threshold: 30.0 s. Each file also has shorter
quiet intervals, listed in `l2_integrity_report.json`. The archive has no heartbeat, so
**a loss inside a quiet interval would be unobservable**, and the end-of-file snapshot is a
single checkpoint. Tardis.dev files have no update IDs, so their sequence continuity
cannot be tested at all.

## Replay policy

`BybitContinuityValidator` keeps its strict setting: an increment other than +1 invalidates
the book until the next snapshot. That is a conservative resync policy, not a claim that
Bybit promises +1. If the live stream legitimately skips IDs, it fails safe by
blocking signals, and it can be switched off with `book.require_contiguous_update_id`.

## Live requirements (not implemented in this demo)

* On any new `snapshot`, reset the book, as Bybit documents.
* Treat `u` = 1 as a restart.
* Resubscribe on disconnect and rebuild from the fresh snapshot.
* Compare periodic REST or level-1 state with the local book.
* Record local receive times, so the replay of live data can be fully observed.
