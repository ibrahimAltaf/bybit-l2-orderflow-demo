# Client Summary: Bybit L2 Feasibility Demonstration

This repository was written specifically for this pre-contract demonstration. It contains
signal-research code only. It has no execution, risk or exit logic, and your existing
engine is not touched. **It does not claim historical profitability.**

## 1. Historical data

* **Source:** Bybit's official public archive. It is free and needs no account.
  * Order book: `quote-saver.bycsi.com/orderbook/linear/{SYMBOL}/{DATE}_{SYMBOL}_ob200.data.zip`
  * Trades: `public.bybit.com/trading/{SYMBOL}/{SYMBOL}{DATE}.csv.gz`
* **Checked by this code:** each file was downloaded, SHA-256 hashed, and every record parsed.

  | Symbol | Day | Book | Deltas | `u` increments ≠ +1 | Rebuilt book vs. end-of-file snapshot |
  |---|---|---|---:|---:|---|
  | ORDIUSDT (smaller cap) | 2026-10-01 | ob200 | 339,154 | 0 | identical, 200 + 200 levels |
  | 10000LADYSUSDT (delisted 2025-10-06) | 2025-10-01 | ob200 | 553,441 | 0 | identical, 200 + 200 levels |
  | 10000LADYSUSDT | 2025-06-01 | ob500 | 372,857 | 0 | identical, 500 + 500 levels |

* **How to read these results:**
  * **`u` (update ID).** Bybit documents `u` as an update ID, with `u = 1` signalling a
    restart. It does **not** document that `u` rises by exactly 1. We observed that on
    every delta, and our validator treats any other increment as a gap.
  * **End-of-file snapshot.** Each file ends with a snapshot stamped at the next 00:00 UTC
    that carries the same `u` as the last delta. Rebuilding the book from the opening
    snapshot plus all deltas reproduces that snapshot exactly. Bybit does not document
    whether the matching engine or the archiving process produces it.
  * **Trade side.** The archive's trade `side` column behaves as the **taker** side.
    91–96 % of trades printed at the opposite best price, against 1.3–3.1 % on the "maker"
    side. Tardis.dev's independent capture reports the same side for all 21,347 ORDIUSDT
    trades.

## 2. Demonstrated on real data (ORDIUSDT, 2026-10-01, full UTC day)

* **Ingestion and validation.** Schema-validated ingestion. Corrupt or truncated archives
  and malformed records stop the run instead of being skipped.
* **Local L2 book.** Handles snapshot reset, deltas and delete-on-zero, with continuity
  rules for gaps, duplicates, stale updates, resync, crossed or locked books and timestamp
  regression.
* **Event-driven features, with no candles:**
  * CVD and order-flow imbalance over 5 s, 30 s, 120 s and the last 50 trades;
  * liquidity withdrawal and replenishment;
  * absorption;
  * volatility compression and extension.
* **State machine.** A LONG and an exactly mirrored SHORT
  WATCH → CANDIDATE → CONFIRMED machine with 12 named rejection reasons.
* **Replay results:**
  * 360,503 events replayed;
  * 6,086 logged transitions, each with timestamp, update ID, the full feature vector and
    the thresholds applied;
  * 7 naturally occurring CONFIRMED transitions (1 LONG, 6 SHORT).
* **Reproducibility checks:**
  * Two independent processes produced byte-identical output, on one Windows machine.
  * A separate stdlib-only script does not import this package. It rebuilt flow and book
    features from the raw files using only records up to each transition time, and
    matched all 6,086 transitions.

## 3. Status of each component

| Component | Status | Evidence |
|---|---|---|
| Author's prior trading-automation experience | PRE-EXISTING EXPERIENCE | Not part of this repository; described separately by the author |
| Archive download with SHA-256 manifest, integrity checks, retries | IMPLEMENTED FOR FEASIBILITY DEMO | `data/downloader.py`, `tests/unit/test_adversarial.py` |
| Bybit archive parsing and schema validation | VERIFIED ON REAL DATA | `dataset_verification.json` (0 malformed records in 8 files) |
| Incremental L2 reconstruction | VERIFIED ON REAL DATA | 3/3 end-of-file snapshots reproduced exactly |
| Continuity / gap / resync handling | IMPLEMENTED FOR FEASIBILITY DEMO (no real gap occurred) | `test_continuity.py`, `test_adversarial.py`, synthetic `sequence_gap` |
| Trade side as taker side | VERIFIED ON REAL DATA (empirically; not documented for the archive) | `aggressor_side_vs_book`, `provider_cross_check.json` |
| CVD and rolling order flow | VERIFIED ON REAL DATA (independent recomputation) | `independent_recompute.json` |
| Liquidity, absorption, volatility features | IMPLEMENTED FOR FEASIBILITY DEMO | Unit tests; computed on real data, not independently recomputed |
| LONG / SHORT state machine with rejection audit | IMPLEMENTED FOR FEASIBILITY DEMO; run on 1 real day | `demo_events.jsonl`, `signals.csv`, `test_detector.py` |
| Deterministic replay | VERIFIED ON REAL DATA (one machine) | `determinism_check.json` |
| Second provider cross-check (Tardis.dev free day) | VERIFIED ON REAL DATA (one day) | `provider_cross_check.json` |
| Multi-symbol / multi-day coverage scan | PLANNED FOR AWARDED PROJECT | — |
| Live WebSocket ingestion and resync | PLANNED FOR AWARDED PROJECT | — |
| Open interest, funding, liquidations | PLANNED FOR AWARDED PROJECT | — |
| Threshold calibration, out-of-sample validation | PLANNED FOR AWARDED PROJECT | — |
| Integration with your bot | PLANNED FOR AWARDED PROJECT (signals only) | — |
| Execution, risk, exits (TP1 / TP2 / Chandelier / break-even) | NOT INCLUDED (your existing engine, unchanged) | — |
| Profitability or backtest results | NOT INCLUDED | — |

## 4. Reproduce

```bash
git clone https://github.com/ibrahimAltaf/bybit-l2-orderflow-demo.git && cd bybit-l2-orderflow-demo
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
python scripts/run_demo.py        # ~6-12 min, ~70 MB download, ends with DEMO RESULT: PASS/FAIL
```

## 5. Limitations

* This is one replayed symbol-day.
* The thresholds are a-priori and untuned.
* The ob200 book arrives in 100 ms batches, so splitting liquidity loss into executions
  and cancellations is approximate.
* Strict `u` + 1 continuity was observed, not documented.
* No OI, funding or liquidation data is included.
* No out-of-sample test has been run.

Full list: `docs/LIMITATIONS.md`. The adversarial self-review is in
`docs/ADVERSARIAL_REVIEW.md`.
