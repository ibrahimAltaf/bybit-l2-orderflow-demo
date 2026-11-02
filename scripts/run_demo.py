"""One-command feasibility demonstration.

    python scripts/run_demo.py                  # full run (~6-12 min; first run downloads ~70 MB)
    python scripts/run_demo.py --skip-verify    # reuse data/evidence/dataset_verification.json

Every PASS/FAIL below is computed from the evidence files this run writes to
data/evidence/; nothing is hard-coded. The synthetic section at the end uses
labelled test fixtures and does not contribute to the result.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))  # for tests.fixtures.synthetic (fixture code lives under tests/)

from rich.console import Console  # noqa: E402
from tests.fixtures.synthetic import scenarios  # noqa: E402
from tests.helpers import run_events  # noqa: E402

from bybit_demo import __version__  # noqa: E402
from bybit_demo.audit.evidence import config_fingerprint  # noqa: E402
from bybit_demo.audit.logger import setup_logging  # noqa: E402
from bybit_demo.cli import (  # noqa: E402
    NO_CONFIRMATION,
    generate_evidence,
    run_verification,
    write_manifest,
)
from bybit_demo.config import DemoConfig, load_config  # noqa: E402
from bybit_demo.data.downloader import ManifestStore, sha256_file  # noqa: E402
from bybit_demo.data.provider_registry import dataset_files  # noqa: E402
from bybit_demo.errors import BybitDemoError  # noqa: E402
from bybit_demo.models.events import us_to_iso  # noqa: E402

console = Console(highlight=False)
CONFIG = "config/demo.yaml"
STAGES = 10


class Demo:
    def __init__(self, cfg: DemoConfig) -> None:
        self.cfg = cfg
        self.ev = cfg.paths.evidence_dir
        self.results: list[tuple[str, bool]] = []
        self.n = 0

    def load(self, name: str) -> Any:
        return json.loads((self.ev / name).read_text(encoding="utf-8"))

    def stage(self, title: str, body: Callable[[], bool]) -> bool:
        self.n += 1
        console.print(f"\n[bold][{self.n}/{STAGES}] {title}[/bold]")
        try:
            ok = body()
        except BybitDemoError as exc:
            console.print(f"    [red]error:[/red] {exc}")
            ok = False
        console.print(f"    -> {'[green]PASS' if ok else '[red]FAIL'}")
        self.results.append((title, ok))
        return ok


def line(text: str) -> None:
    console.print(f"    {text}")


def synthetic_demo(out_dir: Path) -> None:
    console.print(
        "\n[bold magenta]SYNTHETIC STATE-MACHINE FIXTURES (not market data, not evidence)"
    )
    line(
        "Symbol SYNTHUSDT, dated 2000-01-01, generated in tests/fixtures/synthetic/. They only "
        "show each state-machine path; they do not affect DEMO RESULT."
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "synthetic_transitions.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
        for factory in scenarios.ALL:
            sc = factory()
            _, sink = run_events([sc.book, sc.trades])
            path = [
                f"{r.direction.value} {r.previous_state.value}->{r.new_state.value}"
                + (f"[{','.join(f.value for f in r.rejection_flags)}]" if r.rejection_flags else "")
                for r in sink.transitions
                if not (r.new_state.value == "IDLE" and "cooldown_elapsed" in r.reasons)
            ]
            line(f"SYNTHETIC {sc.name}: {' | '.join(path)}")
            for r in sink.transitions:
                fh.write(json.dumps({"scenario": sc.name, "SYNTHETIC": True, **r.to_dict()}) + "\n")
    line(f"written to {(out_dir / 'synthetic_transitions.jsonl').as_posix()} (git-ignored)")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--skip-verify", action="store_true", help="reuse prior verification output")
    ap.add_argument("--skip-synthetic", action="store_true")
    args = ap.parse_args()
    setup_logging("WARNING")
    cfg = load_config(Path(CONFIG))
    demo = Demo(cfg)
    console.print("[bold]Bybit L2 feasibility demonstration[/bold] (real historical data)")

    def environment() -> bool:
        line(f"Python {platform.python_version()} on {platform.platform()}")
        line(f"bybit_demo {__version__}; config {CONFIG} sha256={config_fingerprint(cfg)[:16]}")
        return sys.version_info >= (3, 12)

    def download_and_verify() -> bool:
        if args.skip_verify:
            line("--skip-verify: using existing dataset_verification.json")
        else:
            _, _, recs = run_verification(CONFIG)
            fetched = sum(not r.from_cache for r in recs)
            line(f"{len(recs)} files: {fetched} downloaded, {len(recs) - fetched} verified cache")
        ok = True
        for r in demo.load("dataset_verification.json")["datasets"]:
            failed = [k for k, v in r["verdict"]["checks"].items() if not v]
            b = r["book"]
            line(
                f"{r['dataset_id']:34s} {b['snapshots']} snapshots, {b['deltas']:>7,} deltas, "
                f"{r['trades']['rows']:>6,} trades  "
                + ("all checks pass" if not failed else f"FAILED: {failed}")
            )
            ok &= r["verdict"]["pass"]
        return ok

    def hashes() -> bool:
        manifest = ManifestStore(demo.ev / "download_manifest.json")
        ok = True
        n = 0
        for ds in cfg.datasets:
            for f in dataset_files(ds, cfg.paths.raw_dir):
                rec = manifest.get(f.url)
                same = (
                    rec is not None
                    and f.local_path.exists()
                    and (sha256_file(f.local_path) == rec.sha256)
                )
                n += 1
                ok &= same
                if not same:
                    line(f"[red]mismatch or missing[/red]: {f.local_path.as_posix()}")
        line(f"{n} raw files re-hashed; SHA-256 equals download_manifest.json: {ok}")
        return ok

    def reconstruction() -> bool:
        ok = True
        for r in demo.load("dataset_verification.json")["datasets"]:
            b = r["book"]
            last = b["snapshot_checkpoints"][-1]
            ok &= b["checkpoint_matches"] == b["checkpoint_comparisons"] >= 1
            line(
                f"{r['dataset_id']:34s} rebuilt book vs. end-of-file snapshot "
                f"({last['bid_levels']}x{last['ask_levels']} levels): "
                f"{b['checkpoint_matches']}/{b['checkpoint_comparisons']} identical; "
                f"max depth held {b['max_bid_levels']}/{b['max_ask_levels']}"
            )
        return ok

    def continuity() -> bool:
        ok = True
        for r in demo.load("dataset_verification.json")["datasets"]:
            b = r["book"]
            if "update_id_not_plus_one_deltas" in b:
                bad = b["update_id_not_plus_one_deltas"]
                desc = f"u increments other than +1: {bad} (rule: OBSERVED, not documented)"
            else:
                desc = "no update IDs in this source: gaps NOT detectable"
            line(f"{r['dataset_id']:34s} {desc}; continuity failures: {b['continuity_failures']}")
            ok &= b["continuity_failures"] == 0
        return ok

    def trade_flow() -> bool:
        x = demo.load("provider_cross_check.json")["trades"]
        common, mism = x["common_trade_ids"], x["common_ids_with_price_qty_or_side_mismatch"]
        line(f"Bybit vs Tardis.dev trades: {common:,} common IDs, {mism} price/qty/side mismatches")
        ok = mism == 0 and common > 0
        for r in demo.load("dataset_verification.json")["datasets"]:
            a = r["aggressor_side_vs_book"]
            line(
                f"{r['dataset_id']:34s} side vs. prior book: taker-consistent "
                f"{a['taker_consistent_share']:.1%}, "
                f"maker-consistent {a['maker_consistent_share']:.1%}"
            )
            ok &= a["consistent_with_taker_side"] > a["consistent_with_maker_side"]
        return bool(ok)

    def replay() -> bool:
        # Determinism (two processes) is written to determinism_check.json; judged in stage 10.
        generate_evidence(CONFIG, report=False)
        r = demo.load("replay_summary.json")
        s = r["summary"]
        line(f"{s['symbol']} {s['first_ts']} -> {s['last_ts']}")
        line(
            f"{s['events']:,} events ({s['book_events']:,} book, {s['trades']:,} trades) in "
            f"exchange-time order; {s['evaluations']:,} detector evaluations"
        )
        line(
            f"session CVD {float(s['cvd_notional']):+,.2f} USDT; final bid/ask "
            f"{s['final_best_bid']}/{s['final_best_ask']}; malformed records: "
            f"{r['book_schema_validation']['records_malformed']}+"
            f"{r['trades_schema_validation']['records_malformed']}"
        )
        c = demo.load("continuity_report.json")
        return bool(c["status"] == "PASS" and s["events"] > 0)

    def state_machine() -> bool:
        s = demo.load("replay_summary.json")["summary"]
        line(f"{s['transitions']:,} transitions logged with features, thresholds, u and seq")
        confirmed = []
        with (demo.ev / "demo_events.jsonl").open(encoding="utf-8") as fh:
            for raw in fh:
                rec = json.loads(raw)
                if rec.get("new_state") == "CONFIRMED":
                    confirmed.append(rec)
        if not confirmed:
            line(NO_CONFIRMATION)
        for rec in confirmed:
            line(
                f"CONFIRMED {rec['direction']:5s} {us_to_iso(rec['ts_us'])} u={rec['update_id']} "
                f"setup={rec['setup_id']}"
            )
        return bool(s["transitions"] > 0 and len(confirmed) == s["confirmed"])

    def rejections() -> bool:
        s = demo.load("replay_summary.json")["summary"]
        by_prev: dict[str, int] = {}
        for k, v in s["transition_counts"].items():
            if k.endswith("->REJECTED"):
                prev = k.split()[1].split("->")[0]
                by_prev[prev] = by_prev.get(prev, 0) + v
        total = sum(by_prev.values())
        line(
            f"{total:,} REJECTED transitions; from "
            + ", ".join(f"{k} {v:,}" for k, v in sorted(by_prev.items()))
        )
        line("flag occurrences (one rejection may carry several):")
        for k, v in sorted(s["rejection_counts"].items(), key=lambda kv: -kv[1]):
            line(f"  {k:24s} {v:>6,}")
        return total > 0

    def reproducibility() -> bool:
        d = demo.load("determinism_check.json")
        line(
            f"two independent processes, {len(d['files'])} output files: "
            f"{'byte-identical' if d['identical'] else 'DIFFERENT'}"
        )
        proc = subprocess.run(
            [sys.executable, "-I", "scripts/independent_recompute.py"],
            check=False,
            capture_output=True,
            text=True,
        )
        line("independent stdlib recomputation: " + (proc.stdout.strip() or proc.stderr.strip()))
        return bool(d["identical"]) and proc.returncode == 0

    try:
        demo.stage("Environment", environment)
        if demo.stage("Historical data download and verification", download_and_verify):
            demo.stage("Raw-file hash validation", hashes)
            demo.stage("L2 reconstruction vs. exchange snapshot records", reconstruction)
            demo.stage("Continuity validation", continuity)
            demo.stage("Trade flow / aggressor side / CVD inputs", trade_flow)
            if demo.stage("Deterministic event replay (ORDIUSDT 2026-10-01)", replay):
                demo.stage("State-machine evidence", state_machine)
                demo.stage("Rejections", rejections)
                demo.stage("Reproducibility and no-lookahead checks", reproducibility)
    finally:
        write_manifest(cfg.paths.evidence_dir)  # last, so it covers every evidence file

    if not args.skip_synthetic:
        synthetic_demo(cfg.paths.synthetic_output_dir)

    ran_all = len(demo.results) == STAGES
    passed = ran_all and all(ok for _, ok in demo.results)
    console.print("\n[bold]EVIDENCE[/bold] (data/evidence/, hashes in evidence_manifest.json)")
    for p in sorted(cfg.paths.evidence_dir.glob("*.json*")) + sorted(
        cfg.paths.evidence_dir.glob("*.csv")
    ):
        line(f"{p.name:30s} {p.stat().st_size:>12,} B")
    console.print(
        "\nScope: signal-research feasibility only. No profitability claim; thresholds are "
        "a-priori and untuned; one symbol-day replayed. See docs/LIMITATIONS.md."
    )
    failed = [t for t, ok in demo.results if not ok]
    if not ran_all:
        failed.append(f"{STAGES - len(demo.results)} stage(s) not run")
    console.print(
        f"\n[bold]DEMO RESULT: {'[green]PASS' if passed else '[red]FAIL'}[/bold]"
        + ("" if passed else f"  ({'; '.join(failed)})")
    )
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
