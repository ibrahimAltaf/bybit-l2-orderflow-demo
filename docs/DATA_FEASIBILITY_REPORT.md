# Historical Bybit L2 Data — Feasibility Report

Prepared 2026-11-02. Evidence files: `data/evidence/dataset_verification.json`,
`data/evidence/download_manifest.json`, `data/evidence/provider_cross_check.json`.

**Status labels**

| Label | Meaning |
|---|---|
| **VERIFIED** | This repository's code downloaded the file / API response, hashed it, parsed every record and checked it. |
| **OBSERVED** | Measured on the verified files; not a documented provider guarantee. |
| **ENDPOINT RESPONDED** | HTTP request returned 200 (HEAD / range GET) but the file was not downloaded or parsed. |
| **DOCUMENTED BY PROVIDER — NOT PERSONALLY VERIFIED** | Taken from provider documentation only. |
| **INFERRED** / **PLANNED** / **NOT VERIFIED** | As stated. |

## 1. Bottom line

* **VERIFIED:** Bybit's own public archive provides **free, unauthenticated, daily
  incremental L2 files (snapshot + delta, with update ID `u` and cross-sequence `seq`)**
  for Bybit USDT perpetuals, including a smaller-cap contract (ORDIUSDT) and a **delisted**
  contract (10000LADYSUSDT). Matching public trade files carry a reported trade `side`.
  It behaves as the **taker** side: 91.5–95.8 % of trades printed at or through the opposite
  best price, against 1.3–3.1 % on the maker side (**OBSERVED**; the archive column is not
  separately documented by Bybit).
* **VERIFIED:** In all three Bybit files checked, every delta's `u` was exactly the previous
  `u` + 1 (1,265,452 deltas; 0 increments other than +1). Bybit does **not** document +1
  increments: this is an OBSERVED property that our validator enforces as a rule.
* **VERIFIED:** Rebuilding the book from the opening snapshot through every delta
  reproduced the snapshot record at the end of each file **level for level on all
  levels** (200 or 500 per side). That record is stamped at the next 00:00 UTC and carries
  the same `u` as the last delta. Whether the matching engine or the archiver writes it is
  not documented (LIMITATIONS §7).
* **VERIFIED:** An independent third-party capture (Tardis.dev free sample) of the same
  ORDIUSDT day contains the identical 21,347 trades (same IDs, prices, sizes, sides).
* Acquisition cost for the Bybit archive: **$0** (VERIFIED: downloaded without credentials).

## 2. Provider inventory

| Provider | URL | Official / 3rd-party | Dataset | Market type | Symbols actually checked | Date ranges actually checked | Trades? | L2 snapshots? | Incremental L2 deltas? | Sequence metadata? | Delisted symbols? | Cost | Verification status | Evidence | Limitations |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Bybit — orderbook archive | `https://quote-saver.bycsi.com/orderbook/linear/{SYMBOL}/{YYYY-MM-DD}_{SYMBOL}_ob{200\|500}.data.zip` (linked from `https://www.bybit.com/derivatives/en/history-data`) | Official | Daily zip of JSON lines in V5 WebSocket `orderbook.{depth}` format | USDT perpetual (`linear`) | ORDIUSDT, 10000LADYSUSDT (delisted) | 2026-10-01 (ORDI); 2025-10-01 (ob200) and 2025-06-01 (ob500) (LADYS); each full UTC day | — | **Yes** (2 per file: day open + end-of-file record at next 00:00, same `u` as last delta) | **Yes** | **Yes**: `u` (update ID), `seq` (cross sequence), `ts`, `cts` | **Yes** (LADYS, status `Closed`) | Free | **VERIFIED** | `dataset_verification.json`, SHA-256 in `download_manifest.json` | Depth-limited (ob200 now, ob500 before ~2025-08-21); 100 ms push batches; RPI orders excluded |
| Bybit — public trades | `https://public.bybit.com/trading/{SYMBOL}/{SYMBOL}{YYYY-MM-DD}.csv.gz` | Official | Daily gzip CSV of all public trades | USDT perpetual | ORDIUSDT, 10000LADYSUSDT | same three days | **Yes**: `side` (behaves as taker side, OBSERVED), `trdMatchID`, `RPI` flag | — | — | Trade ID only (no `seq` column) | **Yes** | Free | **VERIFIED** | as above | Sub-ms timestamps; no exchange sequence number |
| Bybit V5 REST `instruments-info` | `https://api.bybit.com/v5/market/instruments-info?category=linear&symbol=…` | Official | Contract metadata | USDT perpetual | 10000LADYSUSDT, ORDIUSDT | response of 2026-11-02 | — | — | — | — | Reports `status: Closed`, `deliveryTime 1759741200000` (2025-10-06T09:00Z) for LADYS | Free | **VERIFIED** (response inspected) | Response captured in this report (§5) | Snapshot of current metadata only |
| Tardis.dev — free sample | `https://datasets.tardis.dev/v1/bybit/{incremental_book_L2\|trades}/YYYY/MM/DD/{SYMBOL}.csv.gz` | Third-party | Normalized CSV (`incremental_book_L2`, `trades`) | Bybit derivatives | ORDIUSDT | 2026-10-01 (first-of-month free day) | **Yes** (side reported) | **Yes** (`is_snapshot` rows; 2 snapshots incl. a mid-day re-snapshot at 09:27:02Z) | **Yes** | **No** update IDs in CSV (VERIFIED) | Listed in its metadata API (LADYS `availableTo 2025-10-07`): metadata VERIFIED, data NOT VERIFIED | Free for 1st of each month | **VERIFIED** (sample) | `dataset_verification.json`, `provider_cross_check.json` | No sequence IDs → gaps undetectable from CSV; book max 50 levels observed |
| Tardis.dev — paid | `https://tardis.dev` (order form), docs `https://docs.tardis.dev/historical-data-details/bybit` | Third-party | All days, CSV + raw replay API | Bybit derivatives | — | — | Documented | Documented | Documented | Raw API documented to keep `u`/`seq` | Documented | Prices on order form, **not retrieved**; a third-party CCXT comparison page cites $350–$6,000/month (**NOT VERIFIED**) | **DOCUMENTED BY PROVIDER — NOT PERSONALLY VERIFIED** | Billing FAQ (Academic / Solo CSV-only; Pro / Business add raw replay API; monthly = 4 months of history, quarterly = 12, yearly = 4 years) | No paid key held |
| Other vendors (Kaiko, CoinAPI, Amberdata, …) | — | Third-party | — | — | none | none | — | — | — | — | — | — | **NOT VERIFIED (not investigated)** | — | Not needed: the official archive met every requirement |

The Bybit `history-data` landing page itself was **not** fetched by our tooling (request
timed out; the page is JavaScript-rendered). The file endpoints above were verified
directly, so the landing page is cited only as the place the archive is published.

## 3. Verified sample files

All retrieved 2026-11-01 22:08–22:09 UTC by `bybit_demo.data.downloader` (no credentials).

| Dataset | File | Bytes | SHA-256 | Provider Last-Modified |
|---|---|---:|---|---|
| ORDIUSDT 2026-10-01 book | `2026-10-01_ORDIUSDT_ob200.data.zip` | 13,241,285 | `d7adcf49d110f93fbcc756fcbcce4c3a498e0896bf7a8c2becad7c03acb18558` | Fri, 02 Oct 2026 00:09:07 GMT |
| ORDIUSDT 2026-10-01 trades | `ORDIUSDT2026-10-01.csv.gz` | 923,498 | `a59e3cd00ff8950fe492685dd33bd200bbdbdb668bd173cadf13c64069deec70` | Fri, 02 Oct 2026 01:14:17 GMT |
| 10000LADYSUSDT 2025-10-01 book | `2025-10-01_10000LADYSUSDT_ob200.data.zip` | 15,292,185 | `35efe269e3800329c68ef97a4fa60474fa94e65350129e058be64a17a4164394` | Thu, 02 Oct 2025 00:08:35 GMT |
| 10000LADYSUSDT 2025-10-01 trades | `10000LADYSUSDT2025-10-01.csv.gz` | 1,892,178 | `abed33fe15fe250d11c9ddb875bbd8e987a04e2d1b57be273c710e1155d3fb56` | Thu, 02 Oct 2025 01:11:24 GMT |
| 10000LADYSUSDT 2025-06-01 book | `2025-06-01_10000LADYSUSDT_ob500.data.zip` | 10,440,797 | `8e65c11dd72d9c9f8689e5504b92c24606e30d01594098d774da4e41fdabd0c4` | Mon, 02 Jun 2025 00:05:39 GMT |
| 10000LADYSUSDT 2025-06-01 trades | `10000LADYSUSDT2025-06-01.csv.gz` | 891,157 | `513a14b9385ac3e66b3ae208427e8b0901b5fa12c5872a684fba7eb13c5b855f` | Mon, 02 Jun 2025 01:06:41 GMT |
| Tardis ORDIUSDT 2026-10-01 L2 | `ORDIUSDT.csv.gz` (incremental_book_L2) | 15,236,420 | `57ddc0a59f0c80c147b2ceb434f06f33963d6e01b2db8e7e593a1f5acea72f5d` | — |
| Tardis ORDIUSDT 2026-10-01 trades | `ORDIUSDT.csv.gz` (trades) | 679,623 | `80ba6285673a8a70bd365dbfbea9d68b8c3713a258be35e38b8c6c493baa0908` | — |

### Per-file results (every record parsed; 0 schema errors in all 8 files)

| Dataset | Book period (book clock = `cts`) | Messages | Snapshots | Deltas | `u` increments ≠ +1 | `seq` non-increasing | Max levels / side | Checkpoint (end-of-file snapshot) | Trades | Trade period | Buy / Sell | RPI trades |
|---|---|---:|---:|---:|---:|---:|---:|---|---:|---|---|---:|
| ORDIUSDT 2026-10-01 (ob200) | 2026-09-30T23:59:59.917Z → 2026-10-02T00:00:00.211Z | 339,156 | 2 | 339,154 | **0** | **0** | 200 | **match, 0 mismatched levels** | 21,347 | 00:00:02.071Z → 23:59:25.7617Z | 11,802 / 9,545 | 544 |
| 10000LADYSUSDT 2025-10-01 (ob200) | 2025-10-01T00:00:00.057Z → 2025-10-02T00:00:00.207Z | 553,443 | 2 | 553,441 | **0** | **0** | 200 | **match, 0** | 48,184 | 00:01:00.4178Z → 23:58:59.6166Z | 22,743 / 25,441 | 0 |
| 10000LADYSUSDT 2025-06-01 (ob500) | 2025-06-01T00:00:00.883Z → 2025-06-02T00:00:00.561Z | 372,859 | 2 | 372,857 | **0** | **0** | 500 | **match, 0** | 22,605 | 00:00:05.4979Z → 23:59:48.2534Z | 11,392 / 11,213 | 0 |
| Tardis ORDIUSDT 2026-10-01 | 2026-09-30T23:59:59.815Z → 2026-10-01T23:59:58.775Z | 651,959 | 2 | 651,957 | n/a (no IDs) | n/a | 50 | **match, 0** (mid-day snapshot 09:27:02Z) | 21,347 | same | 11,802 / 9,545 | n/a |

Every Bybit file:
* the first delta's `u` is the snapshot's `u` + 1, and the end-of-file snapshot's `u`
  equals the last delta's `u`;
* `ts` never decreased (`system_ts_regressions: 0`);
* there were no duplicate trade IDs, every trade file was already chronological, and the
  book was never crossed;
* every trade timestamp falls inside the dataset's UTC day, and every book record falls
  within ±60 s of it;
* the book never exceeded the advertised depth.

### Independent re-verification scripts

* `python scripts/verify_l2_integrity.py` is a stdlib-only second implementation. It writes
  `data_references.json`, `DATA_REFERENCES.md` (exact URLs, filenames, sizes, SHA-256,
  retrieval times) and `l2_integrity_report.json`. That report covers:
  * snapshot positions, with each snapshot's `u` and `seq` relative to the previous record;
  * the `u` increment distribution and `seq` ordering;
  * `ts` / `cts` regressions, duplicate records, deletes of absent levels and crossed
    books;
  * quiet intervals, maximum and final depth, and the end-of-file reproduction.
* `python scripts/verify_tardis_trades.py` is the stdlib-only trade comparison. It
  documents its matching rules and writes `tardis_trade_comparison.json`.
  * Result: 21,347 common IDs, 0 only in either source, 0 price/size/side/symbol
    mismatches.
  * Timestamp |diff| ≤ 0.9 ms. Tardis has 1 ms precision; Bybit has 0.1 ms.

## 4. Schema and sequence semantics

**Raw fields observed (VERIFIED, all three Bybit book files):**
`topic, type, ts, cts, data.s, data.b, data.a, data.u, data.seq`, for example

```json
{"topic":"orderbook.200.ORDIUSDT","type":"delta","ts":1790812800516,
 "data":{"s":"ORDIUSDT","b":[["4.622","45.97"]],"a":[["4.624","0.51"],["4.638","2216.51"]],
         "u":26783762,"seq":397071436604},"cts":1790812800506}
```

**Trade CSV columns (VERIFIED):** `timestamp, symbol, side, size, price, tickDirection,
trdMatchID, grossValue, homeNotional, foreignNotional, RPI`.

| Field | Documented semantics (Bybit V5 docs) | What we observed |
|---|---|---|
| `type` | `snapshot` / `delta`; a new snapshot resets the local book | 2 snapshots/file: day open and next-day 00:00 snapshot |
| `u` | "Update ID"; `u == 1` → snapshot after service restart. **No statement that it increases by 1** | +1 per delta on 1,265,452 deltas (OBSERVED); last delta `u` = end-of-file snapshot `u`. Our rule: any other increment = SEQUENCE_GAP |
| `seq` | "Cross sequence"; smaller = generated earlier | Strictly increasing on every delta (OBSERVED) |
| `ts` | System generation time (ms) | Never decreasing (`system_ts_regressions: 0`, all 3 files) |
| `cts` | Matching-engine time (ms) | Never decreasing; `ts − cts` = 0 / 24 / 99 / 399 ms (min / p50 / p99 / max, ORDIUSDT) |
| qty `"0"` | Delete level | Book never exceeded the stream depth (deletions sent for levels leaving the window) |
| Push cadence | Level 200: 100 ms; level 50: 20 ms; level 1000: 200 ms (docs) | On `ts`, consecutive deltas are 98–101 ms apart (ORDIUSDT p0.1 = 98 ms, median 101 ms). On `cts` they can be 3 ms apart: `cts` stamps batch *content*, not publication (`inter_delta_gap_ms_*`) |
| RPI | RPI orders not included in book messages (docs) | 544 RPI trades on ORDIUSDT |
| trade `side` | "Side of taker" is documented for the **WebSocket** field `S`; the archive CSV column is not separately documented | Behaves as taker side: 91.5–95.8 % of trades at/through the opposite best price vs 1.3–3.1 % on the maker side (`aggressor_side_vs_book`); identical sides in Tardis.dev's capture. Used as REPORTED aggressor; 0 inferred |

**Implemented continuity rules** (`book/continuity.py`):
* a delta before any snapshot is rejected;
* a duplicate or stale `u` is skipped;
* any `u` increment other than +1 is a SEQUENCE_GAP (our rule), and the book is invalidated
  until the next snapshot (RESYNC);
* `seq` regression and timestamp regression are flagged;
* a `u == 1` snapshot is a SERVICE_RESTART;
* a crossed or locked book is flagged;
* every snapshot received while in sync is first compared level by level with the
  reconstructed book.

**Three kinds of statement, kept apart:**
* *protocol-documented:* snapshot resets the book; quantity 0 deletes; `u = 1` means
  restart; smaller `seq` is earlier;
* *empirically observed:* +1 increments of `u`; the end-of-file snapshot `u` equals the
  last delta's `u`; `side` behaves as the taker side;
* *our validation rule:* strict +1, which fails safe if the live stream behaves otherwise.

**Reconstruction feasibility: VERIFIED.** On each of the three Bybit days, the book rebuilt
incrementally from the opening snapshot equals the end-of-file snapshot record on every
published level.

## 5. Coverage: smaller, new and delisted contracts

| Question | Finding | Status |
|---|---|---|
| Smaller-cap contract available? | ORDIUSDT (≈ $1.67 M aggressive notional on 2026-10-01) full day downloaded and verified | **VERIFIED** |
| Delisted contract available? | 10000LADYSUSDT: `instruments-info` → `status: Closed`, `deliveryTime: 1759741200000` (2025-10-06T09:00Z). Book + trades for 2025-06-01 and 2025-10-01 downloaded and verified | **VERIFIED** |
| Up to delisting day? | 10000LADYSUSDT book and trade files respond for 2025-10-05 and 2025-10-06; **404** for 2025-10-07 and 2025-10-08 | **ENDPOINT RESPONDED** (not downloaded) |
| Depth change over time | BTCUSDT ob500 present ≤ 2025-08-20, ob200 present ≥ 2025-08-22 (ob500 404 on 2025-08-22) | **ENDPOINT RESPONDED** |
| Archive start | BTCUSDT ob500 responds 2023-02-01, 2023-03-01, 2023-04-01; **404** on 2023-01-01 | **ENDPOINT RESPONDED**; exact start not bisected |
| New listings (first days) | Not checked | **NOT VERIFIED** |
| Whole-universe coverage | `public.bybit.com/trading/` lists hundreds of symbols incl. small `1000…`/`10000…` contracts (directory listing fetched); per-symbol L2 coverage not enumerated | **OBSERVED (listing only)** |

## 6. Independent cross-provider check (ORDIUSDT 2026-10-01)

| Check | Result | Status |
|---|---|---|
| Trades: Bybit archive vs Tardis.dev | 21,347 vs 21,347 rows; **21,347 common trade IDs; 0 only-in-either; 0 price/qty/side mismatches**; timestamp differences < 1 ms (Bybit CSV has 0.1 ms precision, Tardis 1 ms) | **VERIFIED** |
| Top of book at 1,439 minute marks (both on `ts` clock) | **1,383 identical (96.1 %)**; all 56 differences at marks with book activity within ±1 s in at least one stream; all 6 marks quiet in both streams were identical | **VERIFIED** counts; cause (20 ms vs 100 ms cadence) **INFERRED** |
| Tardis channel | Max 50 levels / side and ~1.9× the Bybit message count suggest the 50-level stream | **INFERRED** |

## 7. Cost

| Source | Cost | Status |
|---|---|---|
| Bybit orderbook + trade archives | $0, no account / key | **VERIFIED** (downloaded unauthenticated) |
| Bybit REST market metadata | $0 | **VERIFIED** |
| Tardis.dev first day of each month | $0 | **VERIFIED** |
| Tardis.dev other days | Paid subscription; prices only on order form | **DOCUMENTED BY PROVIDER — NOT PERSONALLY VERIFIED** |

## 8. What remains NOT verified / PLANNED

* Historical open interest, funding, liquidations at the granularity the final project
  needs (Bybit offers these via REST/WS; archive depth **NOT VERIFIED**). **PLANNED.**
* Full-universe enumeration of L2 coverage per symbol/day and gap statistics across many
  days. **PLANNED** (the tooling already does this per file; it needs a batch driver).
* Live WebSocket `u` contiguity (only archive files verified). **PLANNED.**
* Licensing terms for redistributing derived evidence. **NOT VERIFIED.**

## 9. Reproduce

```bash
python -m bybit_demo.cli verify-data          # downloads ~70 MB, writes the evidence above
python -m bybit_demo.cli verify-data --dataset bybit_ORDIUSDT_2026-10-01
```
