# Technical Demonstration Report

Real data: Bybit official archive, **ORDIUSDT USDT perpetual, 2026-10-01 (full UTC day)**.
Synthetic fixtures are used *only* for state-machine unit behaviour and are labelled as such.
**This feasibility demonstration does not claim historical profitability.**

## 1. Reproduce

```bash
python -m venv .venv && .venv\Scripts\activate          # (Linux/macOS: source .venv/bin/activate)
pip install -e ".[dev]"
python scripts/run_demo.py                               # everything, ~6-12 min, ends DEMO RESULT: PASS/FAIL

# or step by step
python -m bybit_demo.cli verify-data                     # download + verify 4 datasets + cross-check
python -m bybit_demo.cli evidence                        # replay, determinism re-run, manifest
python scripts/independent_recompute.py                  # stdlib-only audit of logged features
python -m pytest                                         # full test suite
```

## 2. Implemented components

| Requirement | Implementation | Tested by |
|---|---|---|
| Event ingestion | `data/normalization.py` (Bybit zip/csv, Tardis csv), `data/validators.py` | `test_data.py` (malformed rows, missing fields, strict/lenient, ordering); `test_adversarial.py` (truncated / bit-flipped zip, truncated gzip, duplicate price, strict replay abort) |
| Download provenance | `data/downloader.py` (async httpx, SHA-256 manifest, hash-checked cache, integrity check, retry/backoff, Content-Length, atomic rename) | `test_data.py::test_downloader_*`, `test_adversarial.py` (corrupt cache, changed provider file, 5xx retry, 404, offline, short body, corrupt download) |
| Local L2 reconstruction | `book/reconstruction.py` | `test_orderbook.py` (init, bid/ask update, delete, sort, crossed, bands, microprice) |
| Continuity / resync | `book/continuity.py` (Bybit `u`/`seq`/`ts`; timestamp-only for Tardis) | `test_continuity.py` (duplicate, stale, gap→resync, restart, seq/ts regression, crossed, checkpoint mismatch) |
| Book metrics | spread, mid, microprice, depth by bps band, band imbalance, book pressure | `test_orderbook.py`, `test_features.py` |
| Liquidity flow | add / cancel / executed attribution, withdrawal, replenishment, RPI exclusion | `test_features.py` |
| CVD / trade flow | `flow/cvd.py`, `flow/rolling.py` (5 s / 30 s / 120 s + last-50-trades) | `test_flow.py` (signed, exact CVD, eviction boundary, no leakage) |
| Aggressor | reported side; tick-rule inference implemented, labelled INFERRED (0 used) | `test_flow.py` |
| Absorption, volatility, compression, extension | `features/*.py` | `test_features.py` (incl. step-function regression) |
| State machine | `detector/*` LONG + mirrored SHORT | `test_detector.py` (every transition, every rejection reason, cooldown, mirror) |
| Replay | `replay/engine.py`, `replay/clock.py` | `test_replay.py` (order, ties, laziness, pacing, determinism, prefix invariance, timestamp batching) |
| Audit / evidence | `audit/logger.py`, `audit/evidence.py`, `pipeline.py`, `cli.py` | `test_synthetic_pipeline.py`, `test_cli_pipeline.py` |
| Real-data correctness | — | `test_real_sample.py` (full-day checkpoint, real prefix determinism/causality) |
| Adversarial invariants | — | `test_adversarial.py` (future perturbation ×5 scenarios, stream-order independence, no simultaneous LONG/SHORT confirm, locked book, empty side, resync, hand-calculated CVD, wrong date/depth, timestamp round-trip) |

Test result: **192 passed** with raw data, 188 passed and 4 skipped without (`python -m pytest`, incl. 4 real-data and 29 adversarial tests), branch-aware coverage **92 %**.
`ruff check` and `ruff format --check` clean; `mypy --strict` clean over src, tests and scripts (81 files).
The least-covered module is `data/verification.py` (63 %). Its cross-provider path runs on
the real multi-provider data in `verify-data`, not in unit tests.

## 3. Results on the real sample

From `data/evidence/replay_summary.json` and `continuity_report.json`:

```
DATASET        Bybit official archive, ORDIUSDT, ob200
Period         2026-09-30T23:59:59.917Z -> 2026-10-02T00:00:00.211Z   (book clock = cts)
Events         360,503  (339,156 book, 21,347 trades)
Continuity     PASS  (0 failures; issue log: SNAPSHOT_INIT=1, SNAPSHOT_RESET=1)

BOOK           snapshots 2, deltas applied 339,154, skipped 0, SEQUENCE_GAP events 0 (u != previous u + 1 rule), crossed 0
               closing snapshot vs reconstruction: 1/1 match (all 400 levels)
               final best bid/ask 4.556 / 4.558 (200/200 levels)

ORDER FLOW     trades 21,347 (buy 11,802 / sell 9,545; side REPORTED, inferred 0)
               aggressive buy 877,111.46 USDT, sell 790,700.90 USDT
               session CVD +86,410.55 USDT (+18,678.57 ORDI)

SIGNAL AUDIT   evaluations 350,811 (one per distinct timestamp), transitions 6,086
               CONFIRMED 7 (LONG 1, SHORT 6); REJECTED 2,666 (IDLE 2,416, WATCH 246, CANDIDATE 4)
```

| Transition | LONG | SHORT |
|---|---:|---:|
| IDLE → WATCH | 266 | 227 |
| WATCH → CANDIDATE | 3 | 8 |
| CANDIDATE → CONFIRMED | 1 | 6 |
| WATCH → IDLE (lapsed) | 117 | 119 |
| IDLE → REJECTED | 1,440 | 976 |
| WATCH → REJECTED | 146 | 100 |
| CANDIDATE → REJECTED | 2 | 2 |

Rejection **flag occurrences** (2,787 over 2,666 REJECTED transitions; a record can carry
several): ISOLATED_SPIKE 2,382 · ALREADY_EXTENDED 150 ·
FLOW_REVERSAL 99 · CVD_DIVERGENCE 94 · SPREAD_TOO_WIDE 28 · BOOK_PRESSURE_REVERSAL 21 ·
INSUFFICIENT_HISTORY 6 · STALE_BOOK 6 · CANDIDATE_TIMEOUT 1.
INSUFFICIENT_DEPTH, SEQUENCE_GAP and CROSSED_BOOK never fired on this day (the book always
had > $2,000 within 25 bps and no continuity failure). They are exercised by unit and
synthetic tests.

**Reading these numbers:** they describe how often hand-set rule conditions coincided on
one day of one contract. They are not hit rates, returns or evidence of predictive value.
No forward price outcome was computed.

### 3.1 Naturally occurring CONFIRMED transitions (real data)

| signal_timestamp (UTC) | Direction | Best bid / ask |
|---|---|---|
| 2026-10-01T03:44:39.214000Z | LONG | 4.710 / 4.712 |
| 2026-10-01T07:36:03.408000Z | SHORT | 4.636 / 4.637 |
| 2026-10-01T11:52:58.590000Z | SHORT | 4.611 / 4.614 |
| 2026-10-01T14:50:45.408000Z | SHORT | 4.507 / 4.509 |
| 2026-10-01T15:20:52.356000Z | SHORT | 4.561 / 4.563 |
| 2026-10-01T16:51:36.884000Z | SHORT | 4.485 / 4.488 |
| 2026-10-01T16:56:41.705000Z | SHORT | 4.478 / 4.480 |

### 3.2 One complete audit chain (setup 415, from `demo_events.jsonl`)

```
2026-10-01T03:44:35.894400Z ORDIUSDT LONG  IDLE -> WATCH        u=26831938 seq=397107456891
  reasons=[aggressive_buy_imbalance, buy_flow_accelerating, rolling_cvd_strengthening,
           price_compressed, bid_support_holding, near_touch_book_imbalance_buy]
  bid/ask=4.706/4.707 spread_bps=2.12 depth_min_25bps=$9,668 cvd_5s=+18,162 imb_30s=0.849
  book_imb=0.123 book_pressure=0.089 compression=1.086 ext_up_sigma=1.27 trades_5s=74

2026-10-01T03:44:38.988000Z ORDIUSDT LONG  WATCH -> CANDIDATE    u=26831967
  reasons=[persistent_buy_flow, strong_buy_imbalance, event_window_cvd_agrees,
           book_pressure_buy, bid_replenishment, liquidity_gate_pass]
  bid/ask=4.710/4.712 spread_bps=4.25 cvd_5s=+27,344 imb_30s=0.880 event_imb=0.965
  book_imb=0.474 book_pressure=0.325 ask_depletion=1.045 ext_up_sigma=1.61

2026-10-01T03:44:39.214000Z ORDIUSDT LONG  CANDIDATE -> CONFIRMED u=26831968 seq=397107471100
  signal_timestamp=2026-10-01T03:44:39.214000Z
  reasons=[buy_flow_continuation, ask_liquidity_consumed_or_withdrawn,
           microprice_and_book_pressure_aligned, execution_liquidity_ok, within_extension_cap]
  imb_5s=0.980 microprice_rel=0.444 book_pressure=0.323 ext_up_sigma=1.61 (cap 2.5)
  thresholds: confirm.min_flow_imbalance_short=0.3, confirm.min_resistance_depletion=0.25,
              confirm.min_microprice_rel=0.0, confirm.min_book_pressure=0.0, confirm.max_extension_sigma=2.5

2026-10-01T03:45:39.436000Z ORDIUSDT LONG  CONFIRMED -> IDLE      reasons=[cooldown_elapsed]
```

Each JSONL record also carries the full feature vector (~65 values) and every threshold
of the stage being evaluated.

### 3.3 Rejection examples (real data)

| Time (UTC) | Setup | Transition | Flags | Key values |
|---|---|---|---|---|
| 00:02:01.8381 | SHORT 1 | IDLE→REJECTED | INSUFFICIENT_HISTORY, ALREADY_EXTENDED, ISOLATED_SPIKE | 2 min into replay; ext_down 2.06σ; one print = 99.6 % of 5 s notional |
| 00:09:56.801 | LONG 14 | WATCH→REJECTED | CVD_DIVERGENCE | 30 s CVD +217 USDT while mid fell ≥ 5 bps |
| 00:24:16.4542 | LONG 30 | WATCH→REJECTED | FLOW_REVERSAL | 5 s imbalance −1.0 (a single sell print) |
| 01:28:11.514 | LONG 131 | WATCH→REJECTED | BOOK_PRESSURE_REVERSAL | book pressure −0.431 (limit −0.40) |
| 02:02:55.908 | LONG 197 | WATCH→REJECTED | SPREAD_TOO_WIDE | spread 8.66 bps (limit 8.0) |
| 02:31:44.157 | SHORT 257 | CANDIDATE→REJECTED | CANDIDATE_TIMEOUT | no confirmation within 30 s |
| 06:03:07.8261 | LONG 663 | IDLE→REJECTED | STALE_BOOK, ISOLATED_SPIKE | no book update for > 5 s |

## 4. Evidence files (`data/evidence/`)

| File | Content |
|---|---|
| `download_manifest.json` | URL, bytes, SHA-256, UTC retrieval time, HTTP Last-Modified for all 8 raw files |
| `dataset_verification.json` | Full-file verification of 4 datasets (schema, `u`/`seq`, latency, checkpoints, trades) |
| `provider_cross_check.json` | Bybit vs Tardis.dev: trades by ID, top-of-book per minute |
| `demo_events.jsonl` | Run header (input hashes, config hash), every continuity issue, every transition, footer |
| `signals.csv` | One compact row per transition |
| `continuity_report.json` | Rules applied, counts, every failure (none), snapshot events |
| `replay_summary.json` | Summary counts + schema-validation reports |
| `determinism_check.json` | SHA-256 of the 4 output files from two independent processes: **identical** |
| `independent_recompute.json` | stdlib-only recomputation of 10 features (5 trade-flow, best bid/ask, `u`, gate-band depth both sides) for all 6,086 transitions from the raw files, plus raw-file SHA-256 vs. the JSONL header: **0 mismatches** |
| `run_info.json` | Wall-clock timings (kept out of the deterministic files) |
| `evidence_manifest.json` | SHA-256 of every evidence file |

## 5. No-lookahead argument (tested, not formally proven)

Scope: everything in this section is about the `cts` replay clock. It shows that no later
record was used on that clock. `cts` itself precedes a book message's generation time, so
this is not evidence that the data was available at those times; see
`docs/CAUSAL_REPLAY_TECHNICAL_RESPONSE.md`.

1. Parsers are generators; `heapq.merge` yields one event at a time. A test confirms that
   reading the first merged event pulls exactly one element from the source.
2. Every rolling structure raises `ReplayOrderError` if time moves backwards and evicts
   only relative to the current time.
3. Features are computed after all events with the current timestamp are applied and
   before any later event is read. Detecting the end of a timestamp batch inspects only
   the next event's timestamp.
4. **Prefix invariance** (synthetic and real data): replaying only the first N events
   reproduces, byte for byte, every transition (including all feature values) that the
   full replay emitted before event N. Any use of future data would break this.
5. **Future perturbation:** in every synthetic scenario, replacing every trade after time T
   with a 1000× larger sell leaves every transition at or before T byte-identical.
6. An independent stdlib script that does not import this package recomputes trade-flow
   features from the raw trade CSV, and book state from the raw archive zip, using only
   records with timestamp ≤ the transition time. Result: 0 mismatches over 6,086
   transitions × 10 features.
7. A code search found no `shift(-1)`, centered or forward-looking window, forward fill,
   resampling, dataframe, or whole-day statistic.

8. **Real-data causality** (`python scripts/verify_no_lookahead.py`):
   * The real day is replayed and stopped at 2026-10-01T08:00:00Z.
   * It is replayed again with every event after 08:00 falsified: trade sides are flipped
     and sizes multiplied by 1000, and book quantities by 7.
   * In both runs, all 1,875 transitions at or before 08:00 are byte-identical to the
     delivered full-day log. Output: `no_lookahead_verification.json`.
9. **Determinism** (`python scripts/verify_determinism.py`): two separate OS processes
   run `python -m bybit_demo.cli replay`. The SHA-256 of the 4 analytical files is
   compared run against run, and against the delivered logs. Output:
   `determinism_verification.json`.

**Feature update order.** For each event in merged order:
1. The event is applied: a trade updates CVD, the rolling windows and the pending
   execution attribution; a book event goes through the continuity check, then the book,
   then the liquidity-flow accounting and mid-price trackers.
2. When the next event has a later timestamp, the windows are advanced to "now", one
   `FeatureVector` is computed, and the LONG machine and then the SHORT machine evaluate
   it.

Not independently recomputed: the volatility, absorption and liquidity-flow features
(covered by 1–5 only).

## 6. Synthetic state-machine demonstration (NOT evidence)

`tests/fixtures/synthetic/` (symbol `SYNTHUSDT`, timestamps on 2000-01-01):

| Scenario | Observed path |
|---|---|
| long_breakout | LONG IDLE→WATCH→CANDIDATE→CONFIRMED |
| short_breakdown | SHORT IDLE→WATCH→CANDIDATE→CONFIRMED (exact mirror) |
| late_entry | LONG IDLE→REJECTED [ALREADY_EXTENDED] (twice), never CONFIRMED |
| sequence_gap | LONG IDLE→WATCH, then WATCH→REJECTED [SEQUENCE_GAP]; 84 deltas logged and skipped while unsynced |
| flow_reversal | LONG IDLE→WATCH→REJECTED [FLOW_REVERSAL] |

## 7. Threshold policy and change log

All thresholds in `config/demo.yaml` were written before the detector first ran on real
data, and **no threshold value has been changed since**. This is the author's statement:
the repository has no version history from before delivery that could prove it. The
following *logic* corrections
were made after the first 60,000-event smoke run (which had produced 1 CONFIRMED). None
was chosen to raise or lower the number of signals; each fixes a correctness defect:

| # | Found by | Defect | Fix |
|---|---|---|---|
| 1 | Reviewing the smoke run | IDLE-state rejections were raised for flow bursts that did not satisfy the WATCH structure conditions, i.e. "rejecting" non-setups | Gates and invalidations are now checked only for setups that would otherwise enter WATCH |
| 2 | Synthetic `sequence_gap` test | After a gap invalidated the book, features stopped being computed, so an active setup froze instead of being rejected with SEQUENCE_GAP | Features keep computing with `synced=False`; the detector rejects explicitly |
| 3 | Unit test | Extension used the rolling min/max of mid *change points* only, so the mid in force at the window start was dropped (extension under-stated in quiet periods) | Mid treated as a step function (value at window start included) |
| 4 | Independent recomputation script | Detector evaluated after *each* event, so a multi-fill taker sweep sharing one timestamp was judged after its first fill (inflating ISOLATED_SPIKE) | One evaluation per distinct timestamp, after all its events |
| 5 | Inspecting `provider_cross_check.json` | Top-of-book comparison compared price *strings* ("4.620" vs "4.62") | Numeric `Decimal` comparison; added quiet-mark breakdown (77.4 % → 96.1 % identical) |

Observed weaknesses of the a-priori thresholds (deliberately **not** tuned, disclosed):
`ISOLATED_SPIKE` dominates rejections on a contract printing about 0.25 trades/s; the
`min_resistance_withdrawal: 0.0` WATCH branch is close to non-binding; LONG/SHORT counts on
one day mean nothing statistically.

Corrections from the final adversarial review are listed in `docs/ADVERSARIAL_REVIEW.md`.
None changed a threshold or an analytical output value: `demo_events.jsonl` and
`signals.csv` are byte-identical before and after those fixes, apart from the provenance
fields added to the JSONL header.

## 8. Performance and clock sensitivity

**Method.** `python scripts/benchmark_replay.py` replays the full ORDIUSDT day into a
temporary directory. It reports wall time, events/s and peak resident memory (Windows
`PeakWorkingSetSize`). `--profile N` runs cProfile over the first N events.

| Environment | Wall time | Throughput | Peak RSS |
|---|---:|---:|---:|
| Windows 11, Python 3.12.10, Intel Skylake (Family 6 Model 94), 8 logical CPUs, desktop under heavy background load | 195.7 s | 1,842 events/s | 78.5 MB |
| Same machine, lightly loaded (earlier run, before this review) | ≈ 128 s | ≈ 2,800 events/s | — |

**Hot spots** (cProfile, first 60,000 events, own time):
* `FeatureEngine.compute`, run once per distinct timestamp, is about 58 % of the cumulative
  time;
* within it, the depth-band walk (`OrderBook.depth_bands` / `_cumulate`) is about 21 %;
* JSON parsing and `Decimal` validation of the archive are about 12 %.

No optimization was made in this review. The obvious one would cache book metrics between
book updates, but it would only cover the ~3 % of evaluations that follow a trade-only
batch. Memory stays flat because events stream lazily; only the trade file (21k rows) is
held in memory.

**Clock sensitivity.** The demo replays book events on matching-engine time `cts`. A second
replay on publish time `ts` (config `replay.book_clock: ts`) gave:

| | `cts` (evidence) | `ts` (sensitivity run) |
|---|---:|---:|
| evaluations | 350,811 | 350,880 |
| transitions | 6,086 | 6,111 |
| CONFIRMED | 7 | 7 (same 7 setups) |
| CONFIRMED timestamp shift | — | −198 ms … +32 ms |
| ISOLATED_SPIKE flags | 2,382 | 2,391 |

Neither run models live latency, because trades remain on engine time in both. This table
is superseded by the full scenario comparison in `docs/CAUSAL_REPLAY_TECHNICAL_RESPONSE.md`.
