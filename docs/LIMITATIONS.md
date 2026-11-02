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
   Our rebuilt book matched it exactly on all three files. Bybit does not document whether
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
10. **100 ms batching (documented for level 200; the inter-delta gaps were measured, see
    DATA_FEASIBILITY_REPORT §4).**
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
    * Signals are causal on the exchange clock. They do **not** model network or
      processing latency.
15. **Tardis.dev CSV has no update IDs (VERIFIED).**
    * Gaps are undetectable from that source.
    * Top of book agreed with the Bybit archive at 96.1 % of minute marks. The cause of the
      56 disagreements (different push cadence) is **INFERRED**.
    * Tardis.dev paid days and prices are **NOT VERIFIED**.
16. **Archive coverage.**
    * **VERIFIED** for three files: one smaller-cap contract and one delisted contract.
    * The delisted contract's files respond up to the delivery day and return 404 after
      it. This is ENDPOINT RESPONDED (HTTP only, not downloaded).
    * The archive start was probed only by HTTP: BTCUSDT responds on 2023-02-01 and returns
      404 on 2023-01-01. This is ENDPOINT RESPONDED.
    * Listing-day coverage for new contracts and per-symbol gaps across the universe are
      **NOT VERIFIED**.
17. **Open interest, funding, liquidations, regime features, universe selection and
    new-listing handling are PLANNED.** This demo does not ingest them. Historical
    availability of those series is **NOT VERIFIED** here.

## Evidence scope

18. **Independent recomputation covers 10 features** (trade-flow windows, session CVD,
    best bid/ask, `u`, gate-band depth). Volatility, absorption and liquidity-flow
    features are covered by unit tests and future-perturbation tests, not by an
    independent reimplementation.
19. **Determinism was demonstrated on one machine** (Windows 11, Python 3.12.10, two
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
