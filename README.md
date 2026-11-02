# Bybit L2 Feasibility Demonstration

This is a pre-contract technical feasibility demo for Bybit USDT-perpetual **signal research**.
It downloads real historical incremental Level-2 data and trades and verifies them. It then
rebuilds the order book, replays all events deterministically in exchange-time order, and
runs an auditable WATCH → CANDIDATE → CONFIRMED state machine on top.

> **This feasibility demonstration does not claim historical profitability.**
> It contains no execution, risk or exit logic. The client's existing engine
> (TP1 20 % / TP2 20 % / 60 % ATR-Chandelier trail, optional break-even after TP2) is
> neither implemented nor modified.

## Quick start (fresh machine: Python 3.12, git, internet)

```bash
git clone https://github.com/ibrahimAltaf/bybit-l2-orderflow-demo.git
cd bybit-l2-orderflow-demo
python -m venv .venv               # use a Python 3.12 interpreter (e.g. `py -3.12` on Windows)
.venv\Scripts\activate             # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
python scripts/run_demo.py
```

The first run downloads about 70 MB from Bybit and Tardis.dev and takes about 6–12 minutes.
It needs no account, API key or environment variable. The run prints ten numbered stages
and ends with `DEMO RESULT: PASS` or `FAIL`. Every result is computed from the evidence
files it writes to `data/evidence/`.

```
[1/10] Environment
[2/10] Historical data download and verification
[3/10] Raw-file hash validation
[4/10] L2 reconstruction vs. exchange snapshot records
[5/10] Continuity validation
[6/10] Trade flow / aggressor side / CVD inputs
[7/10] Deterministic event replay (ORDIUSDT 2026-10-01)
[8/10] State-machine evidence
[9/10] Rejections
[10/10] Reproducibility and no-lookahead checks
```

## What the evidence shows

* **Data.** The official Bybit archive (snapshot plus deltas carrying `u` / `seq`) was
  downloaded and SHA-256 hashed, and every record was parsed. This covers ORDIUSDT
  2026-10-01 and the **delisted** 10000LADYSUSDT in both the ob200 and older ob500 formats.
* **Reconstruction.** On all three days, the book rebuilt from the opening snapshot plus
  every delta is identical, level for level, to the snapshot record at the end of the file.
  That record carries the same `u` as the last delta.
* **Continuity.** Across 1,265,452 deltas, `u` never increased by anything other than +1.
  Bybit does not document +1 increments. We observed them and enforce them as a rule (see
  [docs/LIMITATIONS.md](docs/LIMITATIONS.md) §6).
* **Trade side.** The archive `side` column behaves as the taker side: 91–96 % of trades
  printed at the opposite best price. Tardis.dev's independent capture has the identical
  21,347 ORDIUSDT trades, with the same IDs, prices, sizes and sides.
* **Replay.** The ORDIUSDT full day gives 360,503 events and 6,086 logged transitions.
  There are 7 naturally occurring CONFIRMED transitions (1 LONG, 6 SHORT). Each record
  carries its timestamp, update ID, all features and all thresholds.
* **Reproducibility.** Two processes produced byte-identical output files. A
  stdlib-only script that does not import this package recomputed 10 logged features for
  all 6,086 transitions from the raw files: 0 mismatches.

## What it does NOT show

* Profitability or edge.
* Threshold quality.
* Behaviour across symbols, regimes or dates.
* Live-latency behaviour.
* Anything about OI, funding or liquidation data, which is not ingested.

See [docs/LIMITATIONS.md](docs/LIMITATIONS.md).

## Commands

Run from the repository root.

```bash
python scripts/run_demo.py                           # everything, with PASS/FAIL
python scripts/run_demo.py --skip-verify             # reuse dataset_verification.json
python -m bybit_demo.cli verify-data                 # download + verify all datasets (+ cross-check)
python -m bybit_demo.cli verify-data --dataset bybit_ORDIUSDT_2026-10-01
python -m bybit_demo.cli replay                      # replay primary dataset -> data/evidence/
python -m bybit_demo.cli replay --out out/ --start 2026-10-01T03:00:00Z --end 2026-10-01T04:00:00Z
python -m bybit_demo.cli evidence                    # replay + second-process replay + manifest
python scripts/independent_recompute.py              # stdlib-only check of logged features
python scripts/benchmark_replay.py                   # runtime / throughput / peak memory
```

`--speed N` paces a replay at N× real time. It is tested with a fake clock but was not run
on the full day.

## Data handling

* Datasets are configured in [config/demo.yaml](config/demo.yaml).
* URLs are built by `src/bybit_demo/data/provider_registry.py`.
* Raw files go to `data/raw/` and are **not committed**; their redistribution is governed by
  the Bybit and Tardis.dev terms.

On every run the downloader handles each file as follows:

1. It reuses a cached file only if its SHA-256 equals the one recorded in
   `data/evidence/download_manifest.json`.
2. Otherwise it downloads to a `.part` file, checks the HTTP status, Content-Length and
   archive integrity, and only then renames the file into place.
3. If a re-download's SHA-256 differs from the recorded hash, it refuses
   (`IntegrityError`): the provider's file changed and the evidence would no longer
   describe it.

Transient network errors, HTTP 429 and 5xx responses are retried with backoff. A 404 fails
immediately. Without a network connection or a verified cache, the run fails with an
explicit message.

## Tests and quality gates

```bash
python -m pytest                         # all tests; real-data tests skip until data is downloaded
python -m pytest -m "not real_data"      # unit + synthetic only (no network)
python -m pytest --cov                   # branch coverage
ruff check src tests scripts && ruff format --check src tests scripts
mypy                                     # strict, covers src/, tests/, scripts/
```

Current results are in [docs/FINAL_VERIFICATION_CHECKLIST.md](docs/FINAL_VERIFICATION_CHECKLIST.md).

## Architecture

```
data/ (download, validate, normalize) -> replay/ (deterministic merge, clock)
   -> book/ (continuity + L2 reconstruction + liquidity metrics) + flow/ (CVD, rolling)
   -> features/ (FeatureVector) -> detector/ (LONG/SHORT state machines) -> audit/ (JSONL, CSV, reports)
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Documents

| Document | Purpose |
|---|---|
| [docs/Client_Guide.pdf](docs/Client_Guide.pdf) | Illustrated client guide: installation, running the demo, results |
| [docs/CLIENT_SUMMARY.md](docs/CLIENT_SUMMARY.md) | One-page summary and component status matrix |
| [docs/DATA_FEASIBILITY_REPORT.md](docs/DATA_FEASIBILITY_REPORT.md) | Providers, URLs, files, hashes, sequence semantics, cost |
| [docs/TECHNICAL_DEMO_REPORT.md](docs/TECHNICAL_DEMO_REPORT.md) | Components, results, audit chains, rejections, change log, performance |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Modules and design decisions |
| [docs/LIMITATIONS.md](docs/LIMITATIONS.md) | Everything that must be disclosed |
| [docs/ADVERSARIAL_REVIEW.md](docs/ADVERSARIAL_REVIEW.md) | Self red-team: findings, resolutions, remaining risk |
| [docs/FINAL_VERIFICATION_CHECKLIST.md](docs/FINAL_VERIFICATION_CHECKLIST.md) | Quality gates, PASS/FAIL |
