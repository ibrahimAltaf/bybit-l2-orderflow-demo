# Adversarial Review

This is a self-review written from the position of a client trying to reject the demo. It was
performed 2026-11-02 over the whole repository. Each documentary claim was traced to code
or an evidence field. Each data-semantics assumption was checked against the Bybit V5 docs:
[orderbook](https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook) and
[trade](https://bybit-exchange.github.io/docs/v5/websocket/public/trade). The
corrections below were then made.

Severity:
* **High**: a claim was false or a defect could corrupt evidence.
* **Medium**: a claim was overstated, or a failure mode was handled unsafely.
* **Low**: a precision or hygiene issue.
* **Info**: a residual risk that is disclosed, not fixed.

| # | Severity | Finding | Evidence | Resolution | Remaining risk |
|---|---|---|---|---|---|
| 1 | High | The README said `verify-data` "re-downloads and checks SHA-256 against `download_manifest.json`". The code did not compare: a re-download, or a cached file with a different hash, silently replaced the recorded hash. | The old `download_file` overwrote the record whenever the hash differed | The downloader was rewritten. A cached copy is reused only if its hash matches. A re-download that differs raises `IntegrityError` and installs nothing. The original retrieval time is kept | None known. Tests: `test_provider_file_changed_raises_integrity_error`, `test_corrupted_cache_is_redownloaded_not_trusted` |
| 2 | High | Archive integrity was not checked. A truncated download or cache file surfaced only at parse time, as a raw `zlib`/`BadZipFile` error or, worse, a shorter "valid" day | The old downloader wrote whatever bytes arrived | `check_archive_integrity` (full zip CRC / gzip end-of-stream) runs before installing any file and before trusting an unrecorded cache. Parsers wrap decompression failures in `CorruptFileError` | None known. Tests: truncated zip, bit-flipped member, truncated gzip, non-gzip `.gz`, corrupt download |
| 3 | High | Found while writing the test for #2: the integrity check selected the format by file name, so on the `*.part` temp file it silently did nothing | `test_corrupt_download_is_never_installed` failed | The final name is passed explicitly, and an unknown extension is now an error, not a skip | None |
| 4 | Medium | There was no retry/backoff, no Content-Length check and no User-Agent. An interrupted connection could not be told apart from a short file | Old downloader | Bounded retry (3 attempts, exponential backoff) on transport errors, 429 and 5xx. A 404 fails at once. Content-Length is checked for identity-encoded bodies. An explicit message is given when offline | Content-Length cannot be checked if a server applies Content-Encoding (the verified archives do not) |
| 5 | Medium | The replay validated leniently. A malformed trade row would have been skipped and silently dropped from CVD. A malformed delta would have surfaced only indirectly, as a `u` gap | `open_streams` used the default `ValidationReport(strict=False)` | The replay is now strict and aborts on the first malformed record. `verify-data` stays lenient so it can count defects, and fails its verdict on any | None. Test: `test_replay_pipeline_aborts_on_malformed_record` |
| 6 | High (claim) | The docs said the archive trade `side` is the taker side "(docs)". Bybit documents "Side of taker" only for the WebSocket field `S`. The archive CSV column is undocumented | Bybit trade docs; archive has no schema page | An empirical check was added: each trade versus the reconstructed book just before it. Over 3 days, 91.5–95.8 % of trades are taker-consistent and 1.3–3.1 % maker-consistent. All 21,347 ORDIUSDT trades have the same side in Tardis.dev's capture. Wording changed to OBSERVED, and a verdict check was added | Interpretation rests on observation, not documentation |
| 7 | Medium (claim) | "0 sequence gaps" rested on an undocumented assumption that `u` increments by exactly 1 | Bybit docs define `u` only as "Update ID", with `u = 1` meaning restart | Reworded everywhere to "0 increments other than +1 (our rule; OBSERVED, not documented)". Evidence keys renamed (`update_id_not_plus_one_deltas`, verdict `update_id_always_plus_one`). `continuity.py` separates documented, observed and our-rule semantics | If live `u` can skip, the rule fails safe (blocks signals) |
| 8 | Medium (claim) | "Rebuilt book equals Bybit's closing snapshot" implied an independent exchange ground truth | The end-of-file snapshot has `u` equal to the last delta's `u` (`u_minus_last_applied_u: 0` in all 3 files). Its producer is undocumented | Reworded to "snapshot record at the end of the file". The `u` relation is now recorded in evidence. LIMITATIONS §7 explains what the match does and does not prove | If the archiver generated it from the same deltas, the match shows consistency with Bybit's own reconstruction, not independence |
| 9 | Medium (claim) | "100 ms batching" was asserted for the replay without measurement. The new measurement shows the 100 ms cadence on the publish clock `ts` (99–101 ms gaps), but **not** on the replay clock `cts`, where consecutive deltas can be 3 ms apart | `inter_delta_gap_ms_publish_ts` vs `inter_delta_gap_ms_replay_clock` in `dataset_verification.json` | Both distributions are now recorded. The docs state that the cadence is a publish-side property. A `ts`-clock sensitivity replay was run (TECHNICAL_DEMO_REPORT §8) | The replay applies a batch at its `cts`, up to 399 ms before it was publishable (disclosed: latency not modelled) |
| 10 | Low (claim) | "`ts` never decreasing" had no evidence field behind it (only `cts` was checked) | `verify_book` | `system_ts_regressions` is measured (0 in all Bybit files) | — |
| 11 | Medium (claim) | "No lookahead: proven" | Prefix-invariance tests are strong but not a proof | Reworded to "tested". A *future-perturbation* test was added for all 5 synthetic scenarios: altering every trade after T leaves all transitions at or before T byte-identical. The independent recomputation was extended from 5 trade-flow features to 10, adding best bid/ask, `u` and depth within the gate band, rebuilt from the raw zip with stdlib code. Raw-file hashes are checked against the evidence header | Volatility, absorption and liquidity-flow features have no independent reimplementation |
| 12 | Low (claim) | Rejection counts read as "rejections". They are flag occurrences: 2,787 flags over 2,666 REJECTED transitions | `replay_summary.json` | The docs and the demo output now show REJECTED transitions by origin state, separately from flag occurrences | — |
| 13 | Low | LONG and SHORT were both in WATCH once (2026-10-01T08:45:00.0414Z). LONG entered WATCH in the same evaluation in which SHORT was then rejected for FLOW_REVERSAL. LONG is evaluated first | `demo_events.jsonl` | Documented as a design property (independent hypotheses). A test proves both cannot CONFIRM on the same feature vector | Not observed for CANDIDATE or CONFIRMED |
| 14 | Low (claim) | "Thresholds fixed before the first real run" cannot be proven: there is no version history | The repository has no commits | Reworded to "author's statement", and the lack of proof is disclosed | The client must take this on trust or rerun with their own thresholds |
| 15 | Low | Timestamps were rendered via `ts_us / 1e6` (float, about 0.5 µs resolution at current epoch) | `models/events.py` | Integer arithmetic for rendering and parsing. A test covers microsecond round-trips. No evidence value changed (outputs byte-identical before and after) | — |
| 16 | Low | A duplicate price inside one message was applied as "last wins" | `normalization._levels` | Now rejected as `SchemaError`. Zero occurrences in 1.27 M real messages | — |
| 17 | Low | A wrong-date or wrong-depth file would have passed verification | `_verdict` | New verdict checks: trades inside the UTC day; book within ±60 s of it; book never exceeding the advertised depth | — |
| 18 | Low | `mypy --strict` covered only the package (39 files); `tests/` and `scripts/` were unchecked | `pyproject.toml` | Strict mypy now covers src, tests and scripts. Typing defects found there were fixed | — |
| 19 | Low | `run_demo.py` continued after a failed verification and printed no overall verdict | Old script | Ten numbered stages, each PASS/FAIL computed from evidence files. Dependent stages are skipped after a failure. `DEMO RESULT: PASS/FAIL` and exit code | — |
| 20 | Low | `download_manifest.json` stored the run-dependent `from_cache` flag | Old `ManifestStore.save` | The flag is excluded from the saved manifest | — |
| 21 | Info | Determinism was shown only on one machine and OS (Windows 11, Python 3.12.10) | `determinism_check.json` | Disclosed (LIMITATIONS §19) | Cross-platform byte equality NOT VERIFIED |
| 22 | Info | Only one symbol-day was replayed. ISOLATED_SPIKE dominates on a thin contract. One WATCH branch is near non-binding | `replay_summary.json` | Disclosed, deliberately not tuned | The thresholds' meaning is untested |
| 23 | Info | Tardis.dev top of book differs at 56 of 1,439 minute marks | `provider_cross_check.json` | All differences coincide with book activity within ±1 s. The cause (cadence) is labelled INFERRED | Not proven |
| 24 | Info | Redistribution of derived evidence (top-of-book prices) | Bybit / Tardis.dev terms not reviewed | Raw data is git-ignored. The two large evidence logs are flagged | Licensing NOT VERIFIED |
| 25 | Info | `scripts/run_demo.py` imports the synthetic fixtures from `tests/` | `run_demo.py` | Disclosed (LIMITATIONS §23) | Deleting `tests/` breaks the demo script |
| 26 | Medium | Found while preparing the git commit: JSON evidence files were written with platform line endings (CRLF on Windows), while git stores LF. The SHA-256 values in `evidence_manifest.json` and `determinism_check.json` would not have matched a clone on another OS | `git add` CRLF warnings on `data/evidence/*.json` | All evidence writers now force LF (`newline="\n"`). `.gitattributes` pins LF text and binary archives. Evidence was regenerated. Test: `test_evidence_files_use_lf_on_every_platform` | Cross-platform byte equality of the replay itself is still NOT VERIFIED (#21) |
| 27 | High (claim) | Found by the client: the replay ordered book records by `cts`, earlier than the message's generation time `ts`, so "no lookahead" held only on that clock | `causal_replay_summary.json` (`baseline_exposure`) | `cts` replay relabelled a non-causal baseline; availability clocks added; all claims qualified | No record of this bot's own receipt times exists, so a fully observed replay of the ob200 archive is impossible |

## Checks performed that found no defect

* **Lookahead search.** No `shift(-1)`, negative indexing into future data, centered
  windows, forward fill, resampling, dataframe or whole-day normalization exists in the
  code. `pandas` is not a dependency. The only "peek" is the next event's timestamp, used
  to detect the end of a timestamp batch. The rolling windows, lagged mid and extrema
  accept only non-decreasing time and evict relative to "now".
* **Ordering.** The merge key is fully specified, `(time, kind, stream, position)`. Output
  is identical when the stream argument order is swapped (tested). No set or dict iteration
  order reaches the output: JSON is written with `sort_keys`, and counters are sorted
  before writing.
* **Book semantics.** Snapshot replaces; quantity 0 deletes; unknown price inserts. A delta
  before a snapshot, a duplicate, or a stale `u` is skipped and logged. A gap invalidates
  the book until the next snapshot. A crossed **or locked** book (`bid >= ask`) is flagged
  and blocks signals (tested). An empty side yields no feature vector (tested). After a
  resync the book equals the new snapshot exactly (tested).
* **CVD.** Hand-calculated case: buy 2 @ 10, sell 1 @ 11, buy 0.5 @ 12 gives CVD +15 USDT /
  +1.5 qty, notional imbalance 15/37 and count imbalance 1/3. Window eviction at exactly
  `now − window` is tested.
* **State machine.**
  * No transition is possible without a named reason.
  * CONFIRMED and REJECTED always pass through a cooldown to IDLE before a new setup, so
    there is no immediate re-entry loop. Setup IDs are unique.
  * CANDIDATE times out after `max_duration_ms`.
  * Gates are re-checked in every active state, which prevents stale-feature use: a stale
    book (> 5 s without an update) rejects.
* **Thresholds.** No strategy constants are hidden in Python. The only numeric literals in
  `detector/` and `features/` are structural: zero comparisons for sign tests, unit
  conversions, a 6-decimal rounding of logged floats, and a 4,096-entry housekeeping bound
  in the replenishment memory. All tunables are in `config/demo.yaml`.
* **Exceptions.** There is no bare `except`, no `except Exception`, and no swallowed error.
* **Hygiene.** There are no credentials, tokens, cookies, absolute local paths or personal
  data in source, docs or evidence. `data/raw/*` and `.env` are git-ignored.

## Verdict after fixes

The limited feasibility claims are supported by code and evidence files: data
availability, reconstruction, continuity on the verified files, trade-side
interpretation, deterministic replay, and auditable state transitions. Item 27 corrects the
earlier causality statement.
Everything beyond that is labelled PLANNED or NOT VERIFIED in LIMITATIONS.md.
