# Final Verification Checklist

Run 2026-11-02 on Windows 11 with Python 3.12.10, after the adversarial review
([ADVERSARIAL_REVIEW.md](ADVERSARIAL_REVIEW.md)). Every result below comes from a command
that was actually executed.

| # | Gate | Result | Evidence / command |
|---|---|---|---|
| 1 | Lint and format | **PASS** | `ruff check src tests scripts`: "All checks passed!". `ruff format --check`: 62 files already formatted |
| 2 | Strict type checking | **PASS** | `mypy` (strict, pydantic plugin; covers `src/`, `tests/` and `scripts/`): "no issues found in 62 source files" |
| 3 | Full test suite | **PASS** | `python -m pytest`: **151 passed**, including 4 real-data tests and 29 adversarial tests |
| 4 | Coverage | **92 %** (branch-aware) | `python -m pytest --cov --cov-branch`. Lowest: `data/verification.py` 63 % (its cross-provider path runs in `verify-data` on real data, not in unit tests) and `cli.py` 69 % |
| 5 | Clean-machine reproduction | **PASS** | Copied exactly the files git would commit (no raw data, caches or venv) into a new directory. Then: fresh `py -3.12 -m venv`, `pip install -e ".[dev]"`, offline tests (146 passed, real-data module skipped), `python scripts/run_demo.py` with **all 8 raw files downloaded cold**: `DEMO RESULT: PASS` in 10 min 31 s. Every downloaded file's SHA-256 equalled the recorded manifest |
| 6 | Demo run twice, evidence compared | **PASS** | Run 1 was the clean clone; run 2 was the working repository (cached data), `DEMO RESULT: PASS` in 12 min 12 s. 9 of 10 evidence files byte-identical: `demo_events.jsonl`, `signals.csv`, `continuity_report.json`, `replay_summary.json`, `dataset_verification.json`, `provider_cross_check.json`, `independent_recompute.json`, `determinism_check.json`, `download_manifest.json`. Only `run_info.json` (wall-clock times, by design) differs |
| 7 | In-run determinism | **PASS** | `determinism_check.json`: two independent processes, 4 output files, SHA-256 identical |
| 8 | Independent no-lookahead recheck | **PASS** | `scripts/independent_recompute.py` (stdlib only, `python -I`): 6,086 transitions × 10 features, 0 mismatches. Raw-file hashes equal the JSONL header |
| 9 | Real-data verification | **PASS** | `dataset_verification.json`: all verdict checks pass for 4 datasets (schema, snapshot/delta, `u` +1, end-of-file snapshot match, depth, UTC-day bounds, taker-side consistency) |
| 10 | Analytical output unchanged by the review fixes | **PASS** | `demo_events.jsonl` body and `signals.csv` are byte-identical to the pre-review run. The JSONL header only gained provenance fields |
| 11 | No secrets or local paths | **PASS** | Scan of source, docs, config and evidence found no credentials, tokens, absolute paths or personal data. `.env` and `data/raw/*` are git-ignored |
| 12 | Raw data and licensing | **PASS (raw) / ATTENTION (derived)** | Raw files are git-ignored and re-downloaded with hash checks. `demo_events.jsonl` and `signals.csv` contain prices, so they are git-ignored and shared privately; redistribution terms are **NOT VERIFIED** (LIMITATIONS §20) |
| 13 | Synthetic data clearly marked | **PASS** | Only under `tests/fixtures/synthetic/` (`SYNTHUSDT`, 2000-01-01, `SYNTHETIC_` prefixes, README marker). Demo output is labelled and written to a git-ignored folder. Test `test_synthetic_fixtures_are_marked` |
| 14 | No profitability claims | **PASS** | Every occurrence of "profit" is a disclaimer. No returns or P&L are computed anywhere |
| 15 | Client execution / risk / exit engine untouched | **PASS** | No such code exists in the repository |
| 16 | Evidence identical across operating systems at the byte level | **PASS (line endings) / NOT VERIFIED (other OS)** | All evidence is written with LF; `.gitattributes` pins LF. After regeneration: 0 CR bytes in evidence, all 10 `evidence_manifest.json` hashes match, content equal to the clean-clone run. A replay on Linux/macOS was not executed |

## Real-data results (identical in both runs)

| Item | Value |
|---|---|
| Bybit deltas verified (3 days) | 1,265,452; 0 `u` increments other than +1 |
| End-of-file snapshot reproduced | 3 / 3 |
| Cross-provider trades | 21,347 common IDs, 0 field mismatches |
| Cross-provider top of book | 1,383 / 1,439 minute marks identical |
| Replay events / evaluations | 360,503 / 350,811 |
| Transitions | 6,086 |
| CONFIRMED | 7 (1 LONG, 6 SHORT) |
| REJECTED transitions / flag occurrences | 2,666 / 2,787 |

## Performance

Full-day replay: 195.7 s, 1,842 events/s, 78.5 MB peak RSS on a heavily loaded desktop
(`scripts/benchmark_replay.py`). Method and hot spots are in TECHNICAL_DEMO_REPORT §8.
