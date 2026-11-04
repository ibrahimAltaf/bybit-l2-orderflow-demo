# Limitations (must be disclosed)

Labels:
* **VERIFIED**: our tooling read and checked the actual file or response.
* **OBSERVED**: measured on the verified files, but not a documented guarantee.
* **INFERRED**: reasoned, not proven.
* **PLANNED**: not built.
* **NOT VERIFIED**: not checked.

## What this demo is not

1. **It is not a strategy and not a backtest.** There is no P&L, fill, fee, slippage,
   latency or position logic. The seven real CONFIRMED transitions on one day are
   engineering output, not evidence of edge. *This feasibility demonstration does not
   claim historical profitability.*
2. **No out-of-sample test exists.** No forward returns were computed, so there is neither
   an in-sample nor an out-of-sample performance figure.
3. **One symbol-day was replayed through the detector** (ORDIUSDT 2026-10-01). Two more
   Bybit days and one Tardis.dev day were verified but not replayed. Nothing here
   generalizes across symbols, regimes or dates.
4. **The thresholds are a-priori, not validated.**
   * They were written before the detector first ran on real data and have not been
     changed since. This is the author's statement: the repository has no version history
     from before delivery that could prove it.
   * The logic corrections made after the first real run are listed in
     TECHNICAL_DEMO_REPORT §7.
   * Some thresholds are effectively non-binding. `watch.min_resistance_withdrawal: 0.0`
     passes whenever the resistance side is merely not being replenished.
   * Others dominate. `ISOLATED_SPIKE` (< 3 trades in 5 s, or one print ≥ 60 % of 5 s
     notional) is the most frequent rejection flag on a contract printing about
     0.25 trades/s.
   * Calibration needs a multi-symbol, out-of-sample study, which is project work.
5. **Your execution / risk / exit engine is not implemented, modelled or modified.**

## Data semantics

6. **`u` continuity has three distinct statuses.**
   * *Protocol-documented:* `u` is an update ID, and `u = 1` means a service restart.
   * *OBSERVED:* `u` rose by exactly 1 on all 1,265,452 deltas in the three verified files.
   * *Our rule:* any other increment is a SEQUENCE_GAP and invalidates the book until the
     next snapshot.

   If the live WebSocket legitimately skips IDs, this rule fails *safe*: it blocks signals.
   It must be confirmed on the live stream.
7. **End-of-file snapshot: OBSERVED, provenance undocumented.** Each archive file ends
   with a snapshot stamped at the next 00:00 UTC whose `u` equals the last delta's `u`.
   In the two 10000LADYSUSDT files its `seq` also equals the last delta's `seq`. In
   ORDIUSDT its `seq` is larger. It therefore restates the book after the last delta
   instead of adding an update (`l2_integrity_report.json`). Our rebuilt book matched it
   exactly on all three files. Bybit does not document whether
   the matching engine or the archiving process produces it. If the archiver produces it
   from the same deltas, the match shows agreement with Bybit's own reconstruction rather
   than independent ground truth.
8. **Trade side meaning (OBSERVED).** Bybit documents "Side of taker" for the WebSocket
   trade field `S`. The archive CSV column `side` is not separately documented. We treat
   it as the taker side because:
   * across the three Bybit days, 91.5–95.8 % of trades printed at or through the opposite
     best price, against 1.3–3.1 % on the maker side (`aggressor_side_vs_book`);
   * Tardis.dev's independent capture reports the same side for all 21,347 common ORDIUSDT
     trades.

   The residual 2–7 % "inside spread / maker side" prints are attributed to the 100 ms book
   batching (**INFERRED**).
9. **Depth-limited book.** The archive is ob200 since about 2025-08-21 and ob500 before
   that. This is ENDPOINT RESPONDED by probing dates; the switch fell between 2025-08-20
   and 2025-08-22. The reconstructed book never exceeded the advertised depth, so levels
   leaving the window are deleted by the feed (OBSERVED). Liquidity beyond 200 levels is
   invisible.
10. **100 ms batching.** Bybit documents a 100 ms push for level 200. We measured it on the
    publish clock `ts`, where consecutive deltas are 98–101 ms apart. On the
    matching-engine clock `cts`, which the replay uses, consecutive deltas can be as little
    as 3 ms apart: `cts` stamps the content of a batch, not its publication (see
    DATA_FEASIBILITY_REPORT §4).
    * Adds and cancels at the same price inside one push net out.
    * *Execution vs. cancellation attribution is therefore an approximation.* A level
      decrease is labelled "executed" only up to the trade quantity printed at that price
      since the previous update; the remainder is labelled "cancelled".
    * Queue position and hidden dynamics cannot be recovered.
11. **RPI orders are excluded from Bybit book messages (documented).** RPI trades (544 of
    21,347 on ORDIUSDT) count in CVD but are never attributed to visible levels.
12. **Only one checkpoint per file.** A corruption that healed within the day would not be
    caught by the checkpoint, though the `u` rule would still flag any jump.
13. **Same-millisecond ordering.**
    * Book `cts` has millisecond precision; trade timestamps have 0.1 ms precision.
    * When a trade and a book update share a timestamp, the trade goes first. This is our
      tie rule, reasoned from "the book at `cts` already reflects fills up to `cts`"; it
      is not an exchange guarantee.
    * The public trade CSV has no `seq`, so the true interleaving inside one millisecond
      is unknowable.
14. **Clock choice.**
    * Replay runs on matching-engine time (`cts` and trade time).
    * A live consumer receives book data later: `ts − cts` was up to 399 ms on ORDIUSDT.
    * `cts` is earlier than a book message's generation time `ts`, so the original replay
      is a **non-causal baseline**. Availability-clock replays are in
      `docs/CAUSAL_REPLAY_TECHNICAL_RESPONSE.md`; their limits are in
      `docs/CAUSAL_REPLAY_LIMITATIONS.md`. No replay here uses this bot's own receipt times.
15. **Tardis.dev CSV has no update IDs (VERIFIED).**
    * Gaps are undetectable from that source.
    * Top of book agreed with the Bybit archive at 96.1 % of minute marks. The cause of the
      56 disagreements (different push cadence) is **INFERRED**.
    * Tardis.dev paid days and prices are **NOT VERIFIED**.
16. **Archive coverage overall.**
    * Downloaded and fully parsed: exactly three book files and three trade files (listed
      with exact URLs and SHA-256 in `data/evidence/DATA_REFERENCES.md`).
    * Nothing else in the archive was downloaded.
17. **Open interest, funding, liquidations, regime features, universe selection and
    new-listing handling are PLANNED.** This demo does not ingest them. Historical
    availability of those series is **NOT VERIFIED** here.

## Coverage: newly listed symbols

* **Verified:** nothing. No newly listed contract was downloaded or parsed.
* **HTTP existence checks only:** none were made for a newly listed contract. The only
  archive-start probe was on BTCUSDT: the ob500 file responds for 2023-02-01, 2023-03-01
  and 2023-04-01, and returns 404 for 2023-01-01. This is ENDPOINT RESPONDED: an HTTP
  status only, not downloaded, and the exact start was not bisected.
* **Possible missing early history (NOT VERIFIED):**
  * whether the archive has a file for a contract's listing day;
  * whether that file starts with a snapshot at listing time or at 00:00 UTC;
  * whether first-day files are complete;
  * whether the archive start date (around 2023-02 for BTCUSDT) applies to all symbols.
* **Consequence:** research that needs first-hours behaviour of new listings cannot rely on
  this demo's evidence. It needs a listing-day scan in the awarded project.

## Coverage: delisted symbols

* **Downloaded and verified:** 10000LADYSUSDT book and trades for 2025-06-01 (ob500) and
  2025-10-01 (ob200). Every record was parsed, `u` was +1 on every delta, and the
  end-of-file snapshot was reproduced. The Bybit `instruments-info` response reported
  `status: Closed` and `deliveryTime` 2025-10-06T09:00Z (response inspected 2026-11-02).
* **Verified availability:** files for a contract that is now closed remain downloadable
  (the two days above).
* **Post-delisting behaviour, from HTTP status only (ENDPOINT RESPONDED):**
  * book and trade files respond for 2025-10-05 and 2025-10-06 (the delivery day);
  * they return 404 for 2025-10-07 and 2025-10-08.
  * Not downloaded: whether the delivery-day file ends at delivery time or contains an
    abnormal end.
* **Inferred, not verified:**
  * that other delisted contracts are retained the same way;
  * that no file in between is missing;
  * that the archive is never purged later.

## Missing historical data

* **Within the three verified files:**
  * no `u` increment other than +1;
  * no malformed record, duplicate record, timestamp regression or crossed book.
* **Quiet intervals:** the longest periods without any book message are listed per file
  in `l2_integrity_report.json` (`largest_quiet_intervals`). The archive has no heartbeat,
  so a quiet interval is not proof of missing data. Only the `u` sequence can reveal
  missing updates.
* **Unavailable files:**
  * The downloader treats HTTP 404 as "not published" and fails immediately with
    `DownloadError`, without retrying (tested).
  * The demo then stops with an explicit message. It never substitutes another day,
    another provider, OHLCV or synthetic data.
  * Transient errors (5xx, 429, network) are retried three times with backoff.
* **Unknown coverage:**
  * gaps, days without files, or files with internal `u` jumps for any other symbol or
    date are **NOT VERIFIED**;
  * the awarded project needs a batch scan before using a symbol/date.

## Full project: remaining work (none of these acceptance items has been done or passed)

* **Universe and data:**
  * full-universe research (symbol selection, coverage scan, liquidity filters);
  * open interest, funding and liquidation data;
  * live WebSocket ingestion with gap/resync.
* **Validation protocol:**
  * a frozen-parameter process, with thresholds fixed before any evaluation data is seen;
  * an untouched historical out-of-sample period;
  * at least 100 independent closed trades **per direction** (LONG and SHORT), if
    achievable under the final criteria.
* **Performance measurement, which does not exist in this demo:**
  * separate LONG / SHORT profit factor and expectancy;
  * realistic trading costs (fees, spread, slippage, funding);
  * maximum drawdown;
  * a top-five-winner removal test;
  * missed-move analysis;
  * filter-ablation analysis.
* **Operational:**
  * four full weeks on Bybit Demo Trading;
  * a final reproducibility package;
  * a ten-business-day independent review.

A technically successful replay is not evidence of a profitable or predictive strategy.
This repository makes no such claim.

## Evidence scope

18. **Independent recomputation covers 10 features** (trade-flow windows, session CVD,
    best bid/ask, `u`, gate-band depth). Volatility, absorption and liquidity-flow
    features are covered by unit tests and future-perturbation tests, not by an
    independent reimplementation.
19. **Determinism was demonstrated on one machine** (Windows 11, Python 3.12.10 and 3.13.18, two
    processes). Exact `Decimal` arithmetic and sorted JSON make cross-platform equality
    likely, but it is **NOT VERIFIED**. Float features are rounded to 6 decimals in
    evidence files.

## Licensing / redistribution

20. Raw files are **not committed** (`data/raw/*` is git-ignored). They are re-downloaded
    and checked against the recorded SHA-256. Bybit's and Tardis.dev's terms govern any
    redistribution. Whether sharing derived evidence (`demo_events.jsonl` and `signals.csv`
    contain top-of-book prices) is permitted is **NOT VERIFIED**. For that reason those two
    files are git-ignored and are not in the public repository. `run_demo.py` regenerates
    them locally, and their SHA-256 hashes are in `evidence_manifest.json`. They are shared
    with the client privately.

## Engineering

21. **Throughput is pure-Python research speed.** See TECHNICAL_DEMO_REPORT §8 for the
    benchmark and its methodology. A production multi-symbol live engine needs profiling
    and per-symbol processes.
22. **Run commands from the repository root**, because paths in `config/demo.yaml` are
    relative. The scripts in `scripts/` change directory automatically.
23. **The demo script reuses test-fixture code** (`tests/fixtures/synthetic`) for its
    labelled synthetic section. Deleting `tests/` breaks `run_demo.py`.
