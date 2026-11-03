# START HERE: Bybit L2 Feasibility Evidence Package

This file is the single entry point for independent technical verification. Paths refer
to the evidence package; in the git repository, the delivered evidence is under
`data/evidence/` instead of `evidence/`. Every command
below exists in this package and was executed before delivery. Recorded outputs are in
`FINAL_DELIVERY_AUDIT.md`.

## 1. What this package proves

* **Data availability.** Bybit's official public archive provides free daily
  incremental L2 files (an opening snapshot plus deltas carrying `u` and `seq`) and trade
  files for USDT perpetuals. This includes a smaller-cap contract (ORDIUSDT) and a delisted
  one (10000LADYSUSDT). Three days were downloaded, hashed and parsed record by record.
* **Raw integrity of those three files.**
  * Every delta's `u` is exactly the previous `u` + 1.
  * `seq` is strictly increasing between deltas.
  * There are no timestamp regressions, no malformed or duplicate records, and no
    crossed book.
  * The book rebuilt from the opening snapshot plus all deltas reproduces the snapshot at
    the end of each file, level for level.
* **Cross-provider trades.** The independent Tardis.dev capture of ORDIUSDT 2026-10-01
  contains the same 21,347 trade IDs as the Bybit archive, with identical price, size and
  side.
* **Auditable replay.** The full ORDIUSDT day (360,503 events) replays deterministically
  through a LONG/SHORT WATCH → CANDIDATE → CONFIRMED state machine. All 6,086 transitions
  are logged with their reasons, features and thresholds.
* **Reproducibility.** The replay is byte-identical across separate processes and
  across a clean reinstall with a fresh download on the same machine (other machines and
  operating systems were not tested). No-lookahead was checked by an independent stdlib recomputation and by
  prefix and future-perturbation replays.

## 2. What it does NOT prove

* It does not prove profitability, edge or predictive value. **A technically successful
  replay is not a profitable strategy.** No returns, P&L, costs or drawdown are computed.
* Nothing is generalized beyond the replayed day: only one symbol-day was replayed through
  the detector, and the thresholds are untuned research hypotheses.
* No final acceptance criterion has been run or passed. This covers out-of-sample tests,
  ≥ 100 trades per direction, PF and expectancy, Demo Trading, and independent review.
* Coverage of newly listed symbols, of other delisted symbols, of other dates, and of the
  live WebSocket was not verified.

Full list: `docs/LIMITATIONS.md`. Self red-team: `docs/ADVERSARIAL_REVIEW.md`.

## 3. Requirements

* Python **3.12** (3.12.x), git not required, internet for the first run (~70 MB).
* No account, API key or environment variable.
* Tested on Windows 11 / Python 3.12.10. Other operating systems were not tested.

## 4. Install (from the folder that contains this file)

```bash
python -m venv .venv                 # Windows: py -3.12 -m venv .venv
.venv\Scripts\activate               # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
```

## 5. Download and verify the raw data

```bash
python -m bybit_demo.cli verify-data
```

This downloads the 8 raw files (3 Bybit days of book and trades, plus the Tardis.dev book
and trades) into `data/raw/`. It **refuses to continue if any file's SHA-256 differs from
`data/evidence/download_manifest.json`**. Exact URLs, sizes and hashes:
`evidence/DATA_REFERENCES.md` (machine-readable: `evidence/data_references.json`).

| Symbol | UTC date | L2 file (exact URL) | SHA-256 |
|---|---|---|---|
| ORDIUSDT | 2026-10-01 | https://quote-saver.bycsi.com/orderbook/linear/ORDIUSDT/2026-10-01_ORDIUSDT_ob200.data.zip | `d7adcf49d110f93fbcc756fcbcce4c3a498e0896bf7a8c2becad7c03acb18558` |
| 10000LADYSUSDT | 2025-10-01 | https://quote-saver.bycsi.com/orderbook/linear/10000LADYSUSDT/2025-10-01_10000LADYSUSDT_ob200.data.zip | `35efe269e3800329c68ef97a4fa60474fa94e65350129e058be64a17a4164394` |
| 10000LADYSUSDT | 2025-06-01 | https://quote-saver.bycsi.com/orderbook/linear/10000LADYSUSDT/2025-06-01_10000LADYSUSDT_ob500.data.zip | `8e65c11dd72d9c9f8689e5504b92c24606e30d01594098d774da4e41fdabd0c4` |

The trade files follow `https://public.bybit.com/trading/{SYMBOL}/{SYMBOL}{DATE}.csv.gz`.
The exact URLs and hashes are in `evidence/DATA_REFERENCES.md`.

## 6. Commands (run in this order; each writes into `data/evidence/`)

| # | Purpose | Command | Expected result |
|---|---|---|---|
| 6 | Full demo: verify, replay, determinism, independent recompute | `python scripts/run_demo.py` | ten stages PASS, `DEMO RESULT: PASS` (6–12 min) |
| 7 | Raw L2 snapshot/delta integrity (stdlib only, independent code) | `python scripts/verify_l2_integrity.py` | 3 datasets, all checks pass, `update_id_not_plus_one: 0` each |
| 8 | Tardis.dev trade comparison (stdlib only) | `python scripts/verify_tardis_trades.py` | 21,347 common IDs, 0 only-in-either, 0 field mismatches |
| 9 | Complete replay (writes the full logs) | `python -m bybit_demo.cli replay` | 6,086 transitions, 7 CONFIRMED |
| 10 | No-lookahead verification | `python scripts/verify_no_lookahead.py` | recompute 0 mismatches; prefix and perturbed replays identical |
| 11 | Determinism verification (two separate processes) | `python scripts/verify_determinism.py` | all 4 analytical files identical, and identical to delivered |
| 12 | Tests, lint, types | `python -m pytest` · `ruff check src tests scripts` · `mypy` | 151 passed · clean · clean |
| 13 | Compare your regenerated evidence with the delivered evidence | `python scripts/compare_evidence.py --delivered evidence` | every file IDENTICAL except `run_info.json` / `evidence_manifest.json` |

Verify the delivered files themselves: `cd evidence && sha256sum -c evidence_hashes.sha256`.
On Windows without `sha256sum`, run `certutil -hashfile <file> SHA256` per file.

## 7. Expected high-level results

| Item | Value |
|---|---|
| Bybit L2 deltas parsed (3 files) | 1,265,452; `u` increments other than +1: 0 |
| End-of-file snapshot reproduced | 3 / 3 |
| Tardis.dev trade comparison | 21,347 / 21,347 IDs, 0 field mismatches; timestamp diff ≤ 0.9 ms |
| Replay (ORDIUSDT 2026-10-01) | 360,503 events, 350,811 evaluations, 6,086 transitions |
| CONFIRMED | 7 (1 LONG, 6 SHORT) |
| REJECTED transitions / rejection flags | 2,666 / 2,787 |
| Independent recompute | 6,086 transitions × 10 features, 0 mismatches |

**What "0 gaps" means.** Earlier drafts said "zero gaps". Precisely, it means that every
delta in the three files had `u` = previous `u` + 1.
* Bybit documents `u` only as an "update ID" (`u = 1` means restart). It does **not**
  document +1 increments.
* So this is an OBSERVED property of these files, enforced by our own validation rule.
  It is not a Bybit guarantee and says nothing about unchecked files or the live stream.

## 8. Where the complete logs are

* `evidence/complete/demo_events.jsonl` is the complete chronological audit log. It holds
  a run header (input SHA-256s, config hash), then every state transition (WATCH,
  CANDIDATE, CONFIRMED, REJECTED with flags, lapse and cooldown resets) and every
  continuity issue, then a summary footer. Each transition carries its UTC timestamp,
  direction, previous and new state, reasons, flags, update ID, `seq`, all ~65 feature
  values and every threshold.
* `evidence/complete/signals.csv` has one row per transition, with key features.
* Hashes are in `evidence/evidence_hashes.sha256`.

## 9. How determinism and no-lookahead are enforced

See `docs/TECHNICAL_DEMO_REPORT.md` §5 and `docs/ARCHITECTURE.md` (Design decisions):
* **Ordering key:** `(time, trade-before-book, stream, position)`.
* **Clock:** the replay runs on `cts`, the matching-engine time.
* **Evaluation timing:** one detector evaluation per distinct timestamp, after all events
  at that timestamp are applied. Only the next event's *timestamp* is inspected.
* **Rolling windows:** `(now − w, now]`, and they refuse time moving backwards.
* **Output:** JSON with sorted keys and LF line endings, and no wall-clock values in the
  analytical files.

## 10. Known limitations (summary)

* The replay assumes zero latency: it does not model network or processing delay.
* Splitting liquidity loss into executions and cancellations is approximate, because of
  100 ms ob200 batching.
* Open interest, funding and liquidations are not integrated.
* Raw data is not redistributed; download it with step 5.
* Redistribution terms for the derived logs are not verified.

Details: `docs/LIMITATIONS.md`.
