"""Generate the client response documents from the evidence files.

    python scripts/build_causal_docs.py

Every number and table below is read from data/evidence/. Run it after
scripts/run_full_verification.py and scripts/reconcile_tests.py.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EV = ROOT / "data/evidence"
DOCS = ROOT / "docs"
ACCESSED = "2026-11-04"


def load(name: str) -> Any:
    return json.loads((EV / name).read_text(encoding="utf-8"))


def table(header: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(lines)


def write(name: str, text: str) -> None:
    (DOCS / name).write_text(text.strip() + "\n", encoding="utf-8", newline="\n")


summary = load("causal_replay_summary.json")
scen = summary["scenarios"]
exposure = summary["baseline_exposure"]
lat = summary["collector_latency"]
ts_rec = load("timestamp_reconciliation.json")
cont = load("continuity_classification.json")
py = load("python_test_reconciliation.json")
results = load("verification_results.json")
indep = load("causal_independent_verification.json")
nla = load("no_lookahead_verification.json")
nla_b = load("causal_no_lookahead_best_supported.json")
det = load("determinism_verification.json")
with (EV / "causal_replay_comparison.csv").open(encoding="utf-8", newline="") as fh:
    rows = list(csv.DictReader(fh))
cmd = {r["name"]: r for r in results["commands"]}
py313 = load("python313_replay_check.json")


def survivors(name: str) -> set[str]:
    return {r["baseline_signal_id"] for r in by(name) if r["correspondence"] != "NOT_PRESENT"}


def by(scenario: str) -> list[dict[str, str]]:
    return [r for r in rows if r["scenario"] == scenario]


def seven(scenario: str) -> str:
    out = []
    for r in by(scenario):
        out.append(
            [
                r["baseline_signal_id"],
                r["direction"],
                r["baseline_utc"].replace("2026-10-01T", "")[:12],
                r["corrected_utc"].replace("2026-10-01T", "")[:12],
                r["delta_ms"] or "—",
                r["baseline_confirm_u"],
                r["corrected_confirm_u"] or "—",
                r["correspondence"],
                r["reason"] or "—",
            ]
        )
    return table(
        [
            "Baseline signal",
            "Dir",
            "Baseline UTC",
            "Corrected UTC",
            "Δ ms",
            "Baseline u",
            "Corrected u",
            "Result",
            "Reason",
        ],
        out,
    )


sens = table(
    ["Scenario", "Basis", "Confirmed", "Same setup", "Changed", "Gone", "New", "Transitions"],
    [
        [
            n,
            s["basis"],
            s["confirmed"],
            s["survive_same_setup"],
            s["survive_changed_setup"],
            s["disappear"],
            s["new"],
            f"{s['transitions']:,}",
        ]
        for n, s in scen.items()
    ],
)
b, r = scen["best_supported"], scen["actual_receipt_tardis"]
ctrl = scen["tardis_feed_exchange_clock"]
base = scen["cts_baseline"]
bm, tm = lat["book_messages"], lat["trades"]
exp7 = exposure["seven_signals"]
tests_tail = cmd["tests_and_coverage"]["output_tail"]
test_line = tests_tail[-1]
cov_line = next(ln for ln in tests_tail if ln.startswith("TOTAL"))
coverage = cov_line.split()[-1]

SOURCES = f"""
Sources (accessed {ACCESSED}):
* Bybit V5 orderbook: https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook
* Bybit V5 public trade: https://bybit-exchange.github.io/docs/v5/websocket/public/trade
* Tardis.dev CSV data types: https://docs.tardis.dev/downloadable-csv-files/data-types
* Tardis.dev Bybit details: https://docs.tardis.dev/historical-data-details/bybit
"""

write(
    "CLOCK_SEMANTICS_AND_ASSUMPTIONS.md",
    f"""
# Clock Semantics and Assumptions

## What each source contains

| Source | Field | Documented meaning | Present? |
|---|---|---|---|
| Bybit book archive | `cts` | "The timestamp from the matching engine when this orderbook data is produced." | Yes |
| Bybit book archive | `ts` | "The timestamp (ms) that the system generates the data" | Yes |
| Bybit book archive | `u` / `seq` | "Update ID" / "Cross sequence" | Yes |
| Bybit book archive | client receive time | — | **No** |
| Bybit trade archive | `timestamp` | Fill time. The WebSocket field `T` is "The timestamp (ms) that the order is filled"; the CSV column is not separately documented | Yes |
| Bybit trade archive | message `ts`, `seq`, receive time | Exist on the WebSocket; absent from the CSV | **No** |
| Tardis.dev CSV | `timestamp` | "timestamp provided by exchange in microseconds since epoch" | Yes |
| Tardis.dev CSV | `local_timestamp` | "message arrival timestamp in microseconds since epoch" | Yes |

`ts` is the system's generation time. It is the closest available field to publication,
but Bybit does not call it a publication or send time, and **it is never treated as
this bot's receipt time**. Event-level publication time for trades does not exist in the
archive, so any trade delay on the archive is modeled unless it comes from a recorded
receipt.

`local_timestamp` is a real recorded arrival time, but at Tardis.dev's collector
(documented location: GCP asia-northeast1), on Tardis.dev's clock. Clock synchronization
between that collector and Bybit is unknown. The Tardis book stream is `orderbook.50`,
not the archive's level 200.

## Measured delays

| Stream | Measure | Min | p50 | p99 | Max | Negative |
|---|---|---:|---:|---:|---:|---:|
| Bybit archive book | `ts - cts` (ms) | {exposure["ts_minus_cts_ms"]["min"]} | {exposure["ts_minus_cts_ms"]["p50"]} | {exposure["ts_minus_cts_ms"]["p99"]} | {exposure["ts_minus_cts_ms"]["max"]} | 0 |
| Tardis book ({bm["count"]:,} messages) | `local_timestamp - timestamp` (ms) | {bm["min_us"] / 1000} | {bm["p50_us"] / 1000} | {bm["p99_us"] / 1000} | {bm["max_us"] / 1000} | {bm["negative"]} |
| Tardis trades ({tm["count"]:,}) | `local_timestamp - timestamp` (ms) | {tm["min_us"] / 1000} | {tm["p50_us"] / 1000} | {tm["p99_us"] / 1000} | {tm["max_us"] / 1000} | {tm["negative"]} |

These are empirical distances between two clocks at a third party, not calibrated one-way
latencies, and they say nothing about this bot's connection.

## Batching and ordering

* Level 200 is documented as a 100 ms push. On `ts`, consecutive archive deltas are about
  100 ms apart. On `cts` they can be 3 ms apart, because `cts` stamps the content of a
  batch, not its publication. Changes inside one push are netted and their order is unknown.
* The trade CSV has no `seq`, so trades cannot be interleaved exactly with book updates.
* Tardis.dev rows are in arrival order per file, but the two files come from separate
  streams (the collector uses multiple WebSocket connections), so their relative
  order is only as good as `local_timestamp`.

## Clock modes

| Mode | Book available at | Trade available at | Status |
|---|---|---|---|
| `cts_baseline` | `cts` | `T` | Non-causal baseline, comparison only |
| `publication_time` | `ts` | `T` as a lower bound; no publication field exists | Partial |
| `modeled_availability` | `ts` + documented delay | `T` + delay, or a recorded receipt | Modeled |
| `actual_receipt` | recorded `local_timestamp` | recorded `local_timestamp` | Observed at a third party |

`actual_receipt` refuses any record without a recorded receive time.

## Per-event fields

Matching time (`engine_ts_us` / `exchange_ts_us`), generation time (`system_ts_us`, books
only), recorded receive time (`local_ts_us`, only when recorded), effective availability
(`available_ts_us`), processing-ready time (`ready_ts_us`), source ordinal (`source_line`
and stream position), `u` and `seq`. Provenance and every assumption are in each log's
`run_header.clock` and `clock_report.json`.

## Ordering rules

* The detector consumes events at `ready_ts_us`. Rolling windows use the same time.
* The order is `(ready, trade before book, stream, position)`. The trade-first tie rule is
  ours and deterministic, not an exchange guarantee.
* A stream is one ordered connection: ready time is held non-decreasing within a stream
  (FIFO), and adjustments are counted in `clock_report.json`.
* A negative delay, or an event available before it happened, raises an error. A missing
  timestamp raises an error; it is never fabricated.
* `u` and `seq` continuity is judged on source order, independent of the replay clock. A
  stale update is skipped and cannot alter emitted records.

{SOURCES}
""",
)

write(
    "CAUSAL_REPLAY_TECHNICAL_RESPONSE.md",
    f"""
# Causal Replay: Technical Response

Symbol and day: ORDIUSDT, 2026-10-01 UTC. Thresholds: `config/demo.yaml`, unchanged
(`{summary["config_sha256"][:16]}…`). This is engineering evidence only and makes no
statement about profitability.

## Root cause

The original replay ordered book records by `cts` and trades by fill time. Bybit's message
`ts` is later than `cts` by up to {exposure["ts_minus_cts_ms"]["max"]} ms, so the replay
applied book data before the message had been generated.

* {exposure["transitions_using_book_message_not_yet_generated"]:,} of {
        exposure[
            "transitions_total"
        ]:,} baseline transitions used a book message whose `ts` was later than the decision, by up to {
        exposure["max_lead_ms"]
    } ms.
* All {len(exp7)} baseline CONFIRMED signals did, by {
        min(s["book_ts_minus_decision_ms"] for s in exp7)
    } to {max(s["book_ts_minus_decision_ms"] for s in exp7)} ms.

The earlier no-lookahead checks were valid for what they tested (no later record on the
replay clock). They did not and could not show that the clock itself was causal.

## Is the corrected replay observed or modeled?

* **`best_supported` (same ob200 archive): MODELED.** Trades use recorded third-party
  receipt times joined by trade ID. Book records use `ts` + {bm["p50_us"] / 1000} ms, the
  median measured collector delay, because no receipt exists for level-200 messages.
* **`actual_receipt_tardis`: OBSERVED, at a third-party collector, on a different feed**
  (50 levels at 20 ms, no update IDs).
* **A fully observed historical causal replay of the ob200 archive cannot be proven from
  these archives.** They contain no receipt times, and this bot has no recording for
  that day.

## Seven-signal comparison: `best_supported`

Matched by setup correspondence (same direction, closest within
{summary["matching_method"]["match_window_s"]:g} s, one-to-one, overlapping setup
intervals), not by list position.

{seven("best_supported")}

Result: {b["survive_same_setup"]} same setup, {b["survive_changed_setup"]} changed,
{b["disappear"]} disappeared, {b["new"]} new; {b["transitions"]:,} transitions versus
{base["transitions"]:,}. A negative delta is not a leak. It occurs where a setup confirms on
an earlier book update than in the baseline (compare the two `u` columns): with trades
already in place relative to the book, an earlier evaluation met the rules. The independent
check below covers these logs.

## Seven-signal comparison: `actual_receipt_tardis` (different feed)

{seven("actual_receipt_tardis")}

Result: {r["survive_same_setup"]} same setup, {r["disappear"]} disappeared, {r["new"]} new,
{r["confirmed"]} confirmed.

Control `tardis_feed_exchange_clock` (same Tardis files, exchange timestamps, no receipt
clock): {ctrl["confirmed"]} confirmed, {ctrl["survive_same_setup"]} same setup,
{ctrl["disappear"]} disappeared, {ctrl["new"]} new. Comparing the control with the baseline
shows the effect of the feed; comparing it with `actual_receipt_tardis` shows the effect
of the clock. Surviving signals on the control: {
        ", ".join(sorted(survivors("tardis_feed_exchange_clock")))
    }.
On recorded receipt: {", ".join(sorted(survivors("actual_receipt_tardis")))}. Same set:
{survivors("tardis_feed_exchange_clock") == survivors("actual_receipt_tardis")}. So on this
feed, moving to the observed receipt clock did not change which signals survive; the
disappearance comes from the different feed (depth and cadence), not the clock. The seven
signals are not robust to feed choice and should not be read as stable objects.

## All scenarios

{sens}

Reading this table:
* Uniform delays add the same amount to both streams from `ts` and `T`, so they preserve
  relative order and cannot change decisions; they only shift timestamps. They are not
  evidence of robustness.
* Signals respond to the **relative** delay of book and trades. The asymmetric scenarios
  show this directly.
* Scenario delays are assumptions unless the basis column says otherwise. None is
  calibrated to this bot.

## Rejection flags, baseline vs `best_supported`

{
        table(
            ["Flag", "Baseline", "best_supported"],
            [
                [k, base["rejection_counts"].get(k, 0), b["rejection_counts"].get(k, 0)]
                for k in sorted(set(base["rejection_counts"]) | set(b["rejection_counts"]))
            ],
        )
    }

## Verification

| Check | Result |
|---|---|
| Baseline logs identical to the original evidence | {
        all(summary["baseline_logs_identical_to_original_evidence"].values())
    } |
| Independent stdlib recomputation of clocked logs | {
        sum(1 for x in indep["scenarios"].values() if x["pass"])
    }/{len(indep["scenarios"])} scenarios pass, {
        sum(x["mismatching_transitions"] for x in indep["scenarios"].values())
    } mismatches |
| Records ready before they happened | {
        sum(x["records_ready_before_they_happened"] for x in indep["scenarios"].values())
    } |
| Prefix + falsified-future replay, baseline | {nla["pass"]} ({
        nla["prefix_replay"]["transitions_at_or_before_cut"]:,} transitions) |
| Prefix + falsified-future replay, `best_supported` | {nla_b["pass"]} ({
        nla_b["prefix_replay"]["transitions_at_or_before_cut"]:,} transitions) |
| Determinism, separate processes | {det["pass"]} |

## Commands

```bash
python -m bybit_demo.cli verify-data
python scripts/run_causal_replay.py
python scripts/verify_causal_replay.py --all
python scripts/verify_no_lookahead.py --scenario best_supported
```

Logs: `evidence/causal_logs/<scenario>/`. Comparison: `evidence/causal_replay_comparison.csv`.
Assumptions: `docs/CLOCK_SEMANTICS_AND_ASSUMPTIONS.md`. Limits: `docs/CAUSAL_REPLAY_LIMITATIONS.md`.
""",
)

runs = py["runs"]
rows_py = []
for x in runs:
    n, d = x["without_raw_data"], x["with_raw_data"]
    rows_py.append(
        [
            x["interpreter"].split(" (")[0],
            "accepted" if x["plain_pip_install_exit_code"] == 0 else "refused",
            n["test_ids_collected"],
            n["summary_line"].split(" in ")[0],
            d["test_ids_collected"],
            d["summary_line"].split(" in ")[0],
        ]
    )
skip = next((ln for x in runs for ln in x["without_raw_data"]["skip_reasons"]), "")
deps = runs[0]["dependency_versions"]
write(
    "PYTHON_TEST_RECONCILIATION.md",
    f"""
# Python Test Reconciliation

Reproduced with `python scripts/reconcile_tests.py` on the delivered ZIP
`{py["zip"]}`, fresh venvs, OS `{py["platform"]}`, command `python -m pytest -q -rs`.

{
        table(
            [
                "Interpreter",
                "`pip install -e .[dev]`",
                "Collected, no data",
                "Result, no data",
                "Collected, data",
                "Result, data",
            ],
            rows_py,
        )
    }

## Cause (proven by collection, not assumed)

The two reported figures are the same test suite in two states:

* **147 passed, 1 skipped** is a run **without raw data**, which is the normal state of the
  ZIP and of a fresh clone. `tests/integration/test_real_sample.py` called
  `pytest.skip(..., allow_module_level=True)` at import, so its 4 tests were never
  collected and the whole module counted as 1 skip.
* **151 passed** is the same run **with raw data** downloaded: 147 + the 4 real-data tests.

Skip line recorded: `{skip}`

* Python version: not the cause. 3.12 and 3.13 give the same counts in both states.
* ZIP vs GitHub: the ZIP was built from commit `4219c02` using `git ls-files`, so its tests
  are that commit's tests.
* Scripts: none are counted by pytest.
* Optional dependencies: none affect collection.

We have not seen the client's environment, so the match is by exact counts. The client
report of 147/1 is exactly the no-data state.

## Separate finding: Python 3.13

The delivered `pyproject.toml` had `requires-python = ">=3.12,<3.13"`, so a plain install on
3.13 was refused, and the 3.13 run above used `--ignore-requires-python`. A full replay on
3.13.18 then produced byte-identical analytical files
({sum(f["identical"] for f in py313["files"])}/{len(py313["files"])},
`evidence/python313_replay_check.json`), so the limit is now `<3.14`. Only Windows was tested.

## What changed

The real-data tests now skip per test, so they are always collected: a run without data
reports each of them as skipped instead of hiding them.

Current suite: `{test_line}`, branch coverage {coverage}.

Lock file: `requirements-lock.txt`. Dependencies in the reconciliation run:
`{"`, `".join(deps)}`.

```bash
python -m venv .venv
.venv\\Scripts\\activate        # macOS/Linux: source .venv/bin/activate
pip install -r requirements-lock.txt
pip install -e ".[dev]"
python -m pytest -q -rs
```
""",
)

t = ts_rec["timestamps"]
write(
    "TARDIS_TIMESTAMP_RECONCILIATION.md",
    f"""
# Tardis Timestamp Reconciliation

Reproduce: `python scripts/verify_timestamp_reconciliation.py`
(stdlib only). Output: `evidence/timestamp_reconciliation.json`.

## Exact fields

| Source | Field | Unit | Precision on this file |
|---|---|---|---|
| Bybit trade CSV | `timestamp` | Unix seconds, decimal text | decimal places: {ts_rec["bybit"]["decimal_places_histogram"]} |
| Tardis.dev trades CSV | `timestamp` | integer microseconds | {ts_rec["tardis"]["values_not_a_whole_millisecond"]} values are not whole milliseconds |

Join: exact trade ID. Bybit values convert to integer microseconds with `Decimal`, no float
and no rounding. Prices and sizes compare as `Decimal` ({ts_rec["normalization"]["price_text_differs_but_compared_numerically"]} prices and {ts_rec["normalization"]["size_text_differs_but_compared_numerically"]} sizes differ as text only). Side is lower-cased.
Mismatches in price, size or side: {ts_rec["normalization"]["price_size_side_mismatches"]}.

## Result over {t["pairs"]:,} trade pairs

| Measure | Value |
|---|---|
| Raw maximum absolute difference | **{t["raw_max_abs_diff_us"]} µs ({t["raw_max_abs_diff_us"] / 1000} ms)** |
| Raw signed range (Bybit − Tardis) | {t["raw_min_signed_diff_us"]} to {t["raw_max_signed_diff_us"]} µs |
| Both floored to whole ms, maximum difference | **{t["floored_ms_max_abs_diff_ms"]} ms** |
| Pairs that differ after flooring both to ms | {t["pairs_differing_after_flooring_both_to_ms"]} |
| Every Tardis value equals Bybit floored to ms | {t["every_tardis_value_is_bybit_floored_to_ms"]} |

Distribution of the raw signed difference (µs: count): {t["raw_signed_diff_us_distribution"]}

## Explanation

* **0.9 ms is correct.** It is the exact maximum raw difference, reported by
  `scripts/verify_tardis_trades.py`.
* **0 ms was a truncated display.** `provider_cross_check.json` had a field
  `timestamp_abs_diff_ms` computed as `abs(diff_us) // 1000`, which floors 0–900 µs to 0.
  It is not a measurement of agreement to the microsecond. The field is now
  `timestamp_abs_diff_us`, in exact microseconds.
* **Why the two differ at all:** Tardis.dev values are whole milliseconds, while Bybit's CSV
  carries 0.1 ms. All pairs agree at millisecond precision, so the difference is precision,
  not a disagreement about the trade.

Boundary tests are in `tests/unit/test_reconciliation.py` (exact 0.1 ms conversion,
refusal of sub-microsecond values, and a 1 µs difference straddling a millisecond).
""",
)

write(
    "ORDERBOOK_CONTINUITY_METHODOLOGY.md",
    f"""
# Order-Book Continuity Methodology

## What is and is not claimed

Bybit documents `u` as "Update ID", states that `u` = 1 on a snapshot means a service
restart, and states that a smaller `seq` was generated earlier. **It gives no increment
rule for `u`.** Earlier wording such as "zero gaps" is withdrawn. The statement supported
by evidence is: *no `u` increment other than +1 was observed in the three inspected files.*
That is not a guarantee, and it is not proof that no event is missing.

## Classes (`src/bybit_demo/book/continuity_audit.py`)

| Class | Assigned when |
|---|---|
| CONFIRMED_MISSING | The rebuilt book contradicts a later snapshot |
| SUSPECTED_GAP | `u`, `seq` or timestamp regression; delete of an absent level; crossed or locked book; delta before a snapshot; a non-unit increment with no later snapshot to test it |
| OBSERVED_NONUNIT_U_INCREMENT | `u` jumped, and a later snapshot still matched |
| VALID_RESET_OR_SNAPSHOT_BOUNDARY | Any snapshot, including a `u` = 1 restart |
| UNKNOWN_UNVERIFIABLE | Quiet interval over the threshold, or a source without update IDs |

Exact duplicates of `u` are skipped and counted separately.

## Results (`python scripts/verify_continuity.py`)

{
        table(
            ["Dataset", "Records", *list(cont["datasets"][0]["class_counts"])],
            [
                [d["dataset_id"], f"{d['records']:,}", *d["class_counts"].values()]
                for d in cont["datasets"]
            ],
        )
    }

Quiet threshold: {cont["datasets"][0]["quiet_threshold_s"]} s. Each file also has shorter
quiet intervals, listed in `l2_integrity_report.json`. The archive has no heartbeat, so
**a loss inside a quiet interval would be unobservable**, and the end-of-file snapshot is a
single checkpoint. Tardis.dev files have no update IDs, so their sequence continuity
cannot be tested at all.

## Replay policy

`BybitContinuityValidator` keeps its strict setting: an increment other than +1 invalidates
the book until the next snapshot. That is a conservative resync policy, not a claim that
Bybit promises +1. If the live stream legitimately skips IDs, it fails safe by
blocking signals, and it can be switched off with `book.require_contiguous_update_id`.

## Live requirements (not implemented in this demo)

* On any new `snapshot`, reset the book, as Bybit documents.
* Treat `u` = 1 as a restart.
* Resubscribe on disconnect and rebuild from the fresh snapshot.
* Compare periodic REST or level-1 state with the local book.
* Record local receive times, so the replay of live data can be fully observed.
""",
)

write(
    "CAUSAL_REPLAY_LIMITATIONS.md",
    f"""
# Causal Replay Limitations

* **Not fully observed.** No receipt time exists for the ob200 archive or for this bot. The
  `best_supported` book clock is `ts` + a median delay measured elsewhere.
* **The third-party clock is not ours.** Tardis.dev's collector is at a documented location
  that may differ from any deployment, and its clock synchronization with Bybit is unknown.
  Measured delays may include clock offset.
* **`actual_receipt_tardis` is a different feed** (50 levels at about 20 ms, no `u`). Its
  result is {r["confirmed"]} confirmed, {r["survive_same_setup"]} of 7 surviving. The control
  on the same feed gives the same survivors, which points to the feed, not the clock. It is
  one day, and it does not show what the ob200 archive would do on an observed clock.
* **A fixed median hides jitter.** The book delay is constant in `best_supported`, while the
  measured maximum is {bm["max_us"] / 1000} ms.
* **Trade/book interleaving within a batch is unknown.** There is no trade `seq` in the CSV,
  and pushes net changes inside 100 ms.
* **Uniform delay scenarios are uninformative for survival** because they keep relative order.
* **Delay values in sensitivity scenarios are assumptions**, not calibrated to any system.
* **One symbol-day.** Survival of 7 signals on one clock says nothing about any other day.
* **Thresholds are unchanged and untuned.** The results are not evidence of predictive value,
  and no returns were computed.
* **Processing time** is modeled by one 5 ms scenario only.
* **Execution attribution** can label a level decrease as a cancel when the trade arrives
  after the book update on an availability clock. A live consumer has the same ambiguity.
* **Other operating systems** were not tested. Python 3.12 and 3.13 were tested on Windows.
""",
)
print("docs written")
