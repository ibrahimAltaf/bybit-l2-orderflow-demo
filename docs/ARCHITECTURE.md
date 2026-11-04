# Architecture

Signal-research pipeline only. There is **no execution, risk, position or exit code** in this
repository; the client's existing engine (TP1 20 % / TP2 20 % / 60 % ATR-Chandelier trail,
optional break-even after TP2) is untouched and out of scope.

## Data flow

```
 provider files (zip / csv.gz)                 config/demo.yaml (all thresholds)
        │                                               │
        ▼                                               │
 data/downloader.py ──► download_manifest.json (URL, bytes, SHA-256, UTC time)
        │
        ▼
 data/validators.py + data/normalization.py      (explicit schema checks, Decimal parsing,
        │   lazy generators                       malformed rows counted or fatal)
        ▼
 BookSnapshot / BookDelta / TradeEvent  (models/events.py, epoch-µs UTC, Decimal)
        │
        ▼
 replay/engine.py  merge_streams(): heapq.merge on (time_us, kind_priority, stream, position)
        │          ReplayClock refuses to go backwards; optional wall-clock pacing
        │
        ├── book event ──► book/continuity.py  BookSynchronizer
        │                     ├─ BybitContinuityValidator  (u / seq / ts rules)
        │                     ├─ OrderBook (book/reconstruction.py)
        │                     └─ checkpoint: compare book vs. every later snapshot
        │
        ├── trade event ─► flow/cvd.py (CVD, rolling windows), book/metrics.py (attribution)
        │
        ▼
 features/feature_engine.py  FeatureVector (immutable, point-in-time)
        │
        ▼
 detector/engine.py  LONG + SHORT state machines
   rules.py (WATCH/CANDIDATE/CONFIRM)  invalidation.py (gates + rejection reasons)
        │
        ▼
 audit/logger.py  demo_events.jsonl · signals.csv      audit/evidence.py  continuity_report.json …
```

## Module responsibilities

| Package | Module | Responsibility |
|---|---|---|
| `models` | `events.py` | Normalized `TradeEvent`, `BookSnapshot`, `BookDelta`, `PriceLevel` (frozen, slotted dataclasses). Original Bybit `u`, `seq`, `ts`, `cts`, source line kept. |
| | `orderbook.py` | `TopOfBook` (mid, spread, microprice), `BandDepth`, `LevelChange`. |
| | `signals.py` | `Direction`, `SignalState`, `RejectionReason`, `TransitionRecord`. |
| `data` | `provider_registry.py` | URL patterns that were confirmed by request; local paths. |
| | `downloader.py` | Async streaming download (httpx), SHA-256, manifest, cache by hash. |
| | `validators.py` | Hand-written schema validation, `ValidationReport` (strict / lenient). |
| | `normalization.py` | Bybit archive + Tardis CSV → normalized events. |
| | `verification.py` | The only producer of "VERIFIED" facts: full-file scans, cross-provider check. |
| `book` | `reconstruction.py` | `OrderBook`: dict + bisect-sorted ladders, insert/update/delete, bands. |
| | `continuity.py` | Provider-specific validators, `BookSynchronizer`, issue log. |
| | `metrics.py` | Book metrics; add / cancel / execution / replenishment attribution. |
| `flow` | `rolling.py` | Causal time/count windows, extrema, lagged value. |
| | `cvd.py` | Session CVD and rolling flow windows (exact `Decimal`). |
| | `aggressor.py` | Reported side parsing; explicit tick-rule inference (labelled INFERRED). |
| `features` | `liquidity.py`, `absorption.py`, `volatility.py` | Pure feature functions / trackers. |
| | `feature_engine.py` | Assembles one `FeatureVector` from state already consumed on the replay clock. |
| `detector` | `rules.py`, `invalidation.py`, `states.py`, `engine.py` | State machine. |
| `replay` | `clock.py`, `engine.py` | Deterministic merge and replay. |
| `audit` | `logger.py`, `evidence.py` | JSONL / CSV sinks, JSON logging, evidence manifest. |
| — | `pipeline.py` | Composition root (dependency injection). |
| — | `cli.py` | `verify-data`, `replay`, `evidence`. |

## Design decisions

* **Clock.** Book events are ordered by Bybit `cts` (matching-engine time), the same clock as
  trade fill time `T`. `ts` (publish time) is configurable (`replay.book_clock: ts`).
  On the verified ORDIUSDT day `ts − cts` was 0 / 24 / 99 / 399 ms (min / p50 / p99 / max).
* **Ties (explicit total order).** The sort key is `(time_us, kind, stream_index, position)`:
  * At an identical microsecond a trade precedes a book update. This is our rule, not an
    exchange guarantee: a delta stamped `cts` already contains fills up to `cts`, so the
    fills are known before the level decrease is attributed.
  * Remaining ties break by the stream's argument index and then file position.
  * Trades sharing a timestamp keep file order (stable sort, applied only if the file is
    not already chronological, which it was on every verified file).
  * Swapping the argument order of the book and trade streams gives identical output
    (tested).
  * No ordering depends on dict/set iteration, the filesystem or async completion.
  * The detector is evaluated once per distinct timestamp, after every event carrying it.
    LONG is evaluated before SHORT.
* **No lookahead on the replay clock.** This covers record order on the chosen clock. Whether
  that clock is causal is a separate question: `cts` is not (see
  `docs/CLOCK_SEMANTICS_AND_ASSUMPTIONS.md`).
  * Parsers are generators, and `heapq.merge` pulls one event at a time.
  * Every rolling window raises on time travel.
  * Features are computed after the current timestamp batch is applied and before any later
    event is read. Only the next event's *timestamp* is inspected, to find the end of the
    batch.
  * No dataframe, resampling, centered window, forward fill or whole-day statistic exists
    in the code.
  * Tests, not proofs:
    * *prefix invariance* (synthetic and real data);
    * *future perturbation*: altering every trade after time T leaves all transitions at
      or before T byte-identical;
    * an independent stdlib recomputation from the raw files.
* **Precision.** Book keys, quantities, notionals and CVD are `Decimal`. Ratios fed to
  rules (imbalances, sigmas) are `float`; float window sums are periodically re-summed with
  `math.fsum`. Same input gives the same bytes (verified across two processes on one
  machine; not verified across platforms). Timestamps are rendered with integer arithmetic.
* **Continuity is provider-specific.** Bybit: `u` must increase by exactly 1. This is our
  rule, OBSERVED on the archive and not documented by Bybit; it is a config switch. `seq`
  must be monotonic. Also checked: timestamp regression, snapshot reset, `u == 1` restart.
  Tardis CSV: no IDs → only snapshot/timestamp/crossed checks, reported as a limitation.
* **A gap is never papered over.** On `SEQUENCE_GAP` the book is invalidated, every later
  delta is logged and skipped until a snapshot (`RESYNC`); the detector keeps receiving
  feature vectors flagged `synced=False` and rejects active setups with `SEQUENCE_GAP`.
* **Checkpoint validation.** Each snapshot that arrives while in sync is first compared
  level by level with the reconstructed book. The Bybit daily archive ends with a snapshot
  record stamped at the next 00:00 UTC, carrying the same `u` as the last delta. Who
  produces that record (matching engine or archiver) is undocumented; see LIMITATIONS §7.
* **Corrupt input stops the run.**
  * Truncated or damaged archives raise `CorruptFileError`.
  * The replay validates strictly: the first malformed record raises `SchemaError`.
  * `verify-data` counts malformed records and fails its verdict.
  * A duplicate price within one message is rejected rather than "last wins".
* **State machine.** One `SetupState` per direction. Gates (integrity + liquidity) apply in
  every state; a setup can only be *rejected* if it would otherwise have qualified for
  WATCH. SHORT is generated from LONG by `DirectionalView` (sign flip + side swap), not by a
  second hand-written rule set.
* **Direction independence.** The two machines are independent hypotheses. Both can be
  in WATCH at the same instant: this happened once on the real day, when LONG entered WATCH
  at the same timestamp that SHORT was rejected with FLOW_REVERSAL. Both cannot CONFIRM on
  the same feature vector, because the confirm rule needs a 5 s imbalance of ≥ +0.30 for
  LONG and ≤ −0.30 for SHORT (tested).
* **Rejection priority.** A rejection records *every* flag that fired, in a fixed order:
  integrity gates (SEQUENCE_GAP, CROSSED_BOOK, STALE_BOOK, INSUFFICIENT_HISTORY), then
  liquidity gates (SPREAD_TOO_WIDE, INSUFFICIENT_DEPTH), then invalidations (FLOW_REVERSAL,
  BOOK_PRESSURE_REVERSAL, CVD_DIVERGENCE), then ALREADY_EXTENDED and ISOLATED_SPIKE.
  `signals.csv` lists them in that order.
