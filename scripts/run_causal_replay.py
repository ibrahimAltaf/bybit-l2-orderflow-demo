"""Run the ORDIUSDT 2026-10-01 replay under every clock scenario and compare the signals.

    python scripts/run_causal_replay.py              # all of config/causal_scenarios.yaml
    python scripts/run_causal_replay.py --scenario best_supported --scenario uniform_50ms

Needs the raw files (python -m bybit_demo.cli verify-data). Thresholds come from
config/demo.yaml unchanged. Baseline evidence in data/evidence/ is never written to.

Writes:
  data/evidence/causal_logs/<scenario>/   full logs + clock_report.json per scenario
  data/evidence/causal_replay_comparison.csv
  data/evidence/causal_replay_summary.json
  data/evidence/latency_sensitivity.json
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import zipfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)

from bybit_demo.audit.evidence import config_fingerprint, write_json  # noqa: E402
from bybit_demo.audit.signal_compare import compare_signals, load_setups  # noqa: E402
from bybit_demo.config import DemoConfig, load_config  # noqa: E402
from bybit_demo.data.provider_registry import FileKind, dataset_files  # noqa: E402
from bybit_demo.models.events import us_to_iso  # noqa: E402
from bybit_demo.pipeline import build_clock, load_clock_scenarios, run_replay  # noqa: E402

EVIDENCE = ROOT / "data" / "evidence"
LOGS = EVIDENCE / "causal_logs"
CONFIG = "config/demo.yaml"
SCENARIOS = Path("config/causal_scenarios.yaml")
BASELINE_FILES = (
    "demo_events.jsonl",
    "signals.csv",
    "continuity_report.json",
    "replay_summary.json",
)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def run_one(name: str) -> dict[str, Any]:
    cfg = load_config(Path(CONFIG))
    scenarios, measured = load_clock_scenarios(cfg)
    s = next(x for x in scenarios if x.name == name)
    ds = cfg.dataset(s.dataset) if s.dataset else cfg.primary()
    return run_replay(
        cfg, ds, LOGS / name, build_clock(cfg, s, measured)
    ).summary.deterministic_dict()


def baseline_exposure(cfg: DemoConfig) -> dict[str, Any]:
    """How often did the cts baseline decide using a book message not yet generated?"""
    book = next(
        f.local_path
        for f in dataset_files(cfg.primary(), cfg.paths.raw_dir)
        if f.kind is FileKind.BOOK
    )
    generated_ms: dict[int, tuple[int, int]] = {}
    with zipfile.ZipFile(book) as zf, zf.open(zf.namelist()[0]) as fh:
        for raw in fh:
            m = json.loads(raw)
            generated_ms[m["data"]["u"]] = (m["ts"], m["cts"])
    ahead: list[int] = []
    ahead_signals: list[dict[str, Any]] = []
    total = 0
    with (EVIDENCE / "demo_events.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            if r.get("record_type") != "state_transition":
                continue
            total += 1
            ts_ms = generated_ms[r["update_id"]][0]
            lead_us = ts_ms * 1000 - r["ts_us"]
            if lead_us > 0:
                ahead.append(lead_us)
            if r["new_state"] == "CONFIRMED":
                ahead_signals.append(
                    {
                        "signal": f"{r['direction']}-{r['setup_id']}",
                        "confirmed_utc": r["timestamp"],
                        "book_message_ts_utc": us_to_iso(ts_ms * 1000),
                        "book_ts_minus_decision_ms": lead_us / 1000,
                    }
                )
    latencies = sorted(ts - cts for ts, cts in generated_ms.values())
    return {
        "question": (
            "at each logged transition, was the last applied book message already "
            "generated (ts <= decision time)?"
        ),
        "transitions_total": total,
        "transitions_using_book_message_not_yet_generated": len(ahead),
        "max_lead_ms": max(ahead) / 1000 if ahead else 0,
        "seven_signals": ahead_signals,
        "ts_minus_cts_ms": {
            "min": latencies[0],
            "p50": latencies[len(latencies) // 2],
            "p99": latencies[int(len(latencies) * 0.99)],
            "max": latencies[-1],
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", action="append", help="default: all")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    cfg = load_config(Path(CONFIG))
    scenarios, measured = load_clock_scenarios(cfg)
    names = args.scenario or [s.name for s in scenarios]
    if "cts_baseline" not in names:
        names = ["cts_baseline", *names]
    LOGS.mkdir(parents=True, exist_ok=True)
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        results = dict(zip(names, pool.map(run_one, names), strict=True))

    base_dir = LOGS / "cts_baseline"
    unchanged = {f: sha256(base_dir / f) == sha256(EVIDENCE / f) for f in BASELINE_FILES}
    baseline = load_setups(base_dir / "demo_events.jsonl")
    by_name = {s.name: s for s in scenarios}
    table: dict[str, Any] = {}
    csv_rows: list[dict[str, Any]] = []
    for name in names:
        s, r = by_name[name], results[name]
        cmp = compare_signals(baseline, load_setups(LOGS / name / "demo_events.jsonl"))
        out = LOGS / name
        table[name] = {
            "mode": s.mode.value,
            "basis": s.basis.value,
            "description": s.description,
            "dataset": s.dataset or cfg.primary().id,
            "book_transport_ms": s.book_transport_us / 1000,
            "trade_transport_ms": s.trade_transport_us / 1000,
            "processing_ms": s.processing_us / 1000,
            "events": r["events"],
            "evaluations": r["evaluations"],
            "transitions": r["transitions"],
            "confirmed": r["confirmed"],
            "survive_same_setup": cmp["same_setup"],
            "survive_changed_setup": cmp["changed_setup"],
            "disappear": cmp["not_present"],
            "new": cmp["new_count"],
            "new_signals": [
                {**n, "corrected_utc": us_to_iso(n["corrected_confirmed_us"])}
                for n in cmp["new_signals"]
            ],
            "rejection_counts": r["rejection_counts"],
            "transition_counts": r["transition_counts"],
            "continuity_issue_counts": r["continuity_issue_counts"],
            "log_sha256": {f: sha256(out / f) for f in ("demo_events.jsonl", "signals.csv")},
            "clock_report": (
                json.loads((out / "clock_report.json").read_text(encoding="utf-8"))
                if (out / "clock_report.json").exists()
                else None
            ),
        }
        for row in cmp["rows"]:
            c = row["corrected_confirmed_us"]
            csv_rows.append(
                {
                    "scenario": name,
                    "basis": s.basis.value,
                    "baseline_signal_id": row["baseline_signal_id"],
                    "direction": row["direction"],
                    "baseline_utc": us_to_iso(row["baseline_confirmed_us"]),
                    "corrected_utc": us_to_iso(c) if c is not None else "NOT PRESENT",
                    "corrected_signal_id": row["corrected_signal_id"] or "",
                    "baseline_confirm_u": row["baseline_confirm_u"],
                    "corrected_confirm_u": row["corrected_confirm_u"] or "",
                    "delta_ms": "" if row["delta_ms"] is None else row["delta_ms"],
                    "watch_delta_ms": ""
                    if row["watch_delta_ms"] is None
                    else row["watch_delta_ms"],
                    "candidate_delta_ms": (
                        "" if row["candidate_delta_ms"] is None else row["candidate_delta_ms"]
                    ),
                    "correspondence": row["correspondence"],
                    "reason": row["reason"],
                }
            )
    with (EVIDENCE / "causal_replay_comparison.csv").open("w", encoding="utf-8", newline="\n") as f:
        w = csv.DictWriter(f, fieldnames=list(csv_rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(csv_rows)
    write_json(
        EVIDENCE / "causal_replay_summary.json",
        {
            "symbol": cfg.primary().symbol,
            "utc_date": cfg.primary().date.isoformat(),
            "config_sha256": config_fingerprint(cfg),
            "scenario_file_sha256": sha256(SCENARIOS),
            "baseline_logs_identical_to_original_evidence": unchanged,
            "baseline_exposure": baseline_exposure(cfg),
            "collector_latency": measured,
            "matching_method": compare_signals(baseline, baseline)["method"],
            "scenarios": table,
        },
    )
    write_json(
        EVIDENCE / "latency_sensitivity.json",
        {
            "note": "Sensitivity only. Delays are assumptions unless the basis says otherwise.",
            "scenarios": [
                {
                    k: table[n][k]
                    for k in (
                        "basis",
                        "book_transport_ms",
                        "trade_transport_ms",
                        "processing_ms",
                        "confirmed",
                        "survive_same_setup",
                        "survive_changed_setup",
                        "disappear",
                        "new",
                        "transitions",
                    )
                }
                | {"scenario": n}
                for n in names
            ],
        },
    )
    for n in names:
        t = table[n]
        print(
            f"{n:32s} {t['basis'][:12]:12s} confirmed {t['confirmed']:3d}  "
            f"same {t['survive_same_setup']}  "
            f"changed {t['survive_changed_setup']}  gone {t['disappear']}  new {t['new']}  "
            f"transitions {t['transitions']:,}"
        )
    print("baseline logs identical to original evidence:", all(unchanged.values()))
    return 0 if all(unchanged.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
