"""Command-line interface.

python -m bybit_demo.cli verify-data   # download + verify every configured dataset
python -m bybit_demo.cli replay        # replay the primary dataset -> evidence files
python -m bybit_demo.cli evidence      # replay twice, prove determinism, write manifest
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from bybit_demo.audit.evidence import evidence_manifest, write_json
from bybit_demo.audit.logger import setup_logging
from bybit_demo.config import DemoConfig, load_config
from bybit_demo.data.downloader import DownloadRecord, ManifestStore, sha256_file
from bybit_demo.data.provider_registry import dataset_files
from bybit_demo.data.verification import cross_check, verify_dataset
from bybit_demo.errors import BybitDemoError
from bybit_demo.models.events import us_to_iso
from bybit_demo.pipeline import ReplayResult, ensure_downloaded, run_replay

console = Console()
log = logging.getLogger("bybit_demo")

NO_CONFIRMATION = (
    "No naturally occurring CONFIRMED transition was observed in this verified sample."
)
DETERMINISTIC_FILES = (
    "demo_events.jsonl",
    "signals.csv",
    "continuity_report.json",
    "replay_summary.json",
)


# ----------------------------------------------------------------- helpers
def _load(path: str, overrides: dict[str, Any] | None = None) -> DemoConfig:
    cfg = load_config(Path(path))
    if overrides:
        cfg = cfg.model_copy(update={"replay": cfg.replay.model_copy(update=overrides)})
    return cfg


def _verify_worker(config_path: str, dataset_id: str) -> dict[str, Any]:
    cfg = load_config(Path(config_path))
    manifest = ManifestStore(cfg.paths.evidence_dir / "download_manifest.json")
    ds = cfg.dataset(dataset_id)
    recs = [r for f in dataset_files(ds, cfg.paths.raw_dir) if (r := manifest.get(f.url))]
    return verify_dataset(ds, cfg, recs)


def _cross_worker(config_path: str, a: str, b: str) -> dict[str, Any]:
    cfg = load_config(Path(config_path))
    return cross_check(cfg.dataset(a), cfg.dataset(b), cfg)


def _replay_worker(config_path: str, overrides: dict[str, Any], out_dir: str) -> dict[str, Any]:
    cfg = _load(config_path, overrides)
    res = run_replay(cfg, cfg.primary(), Path(out_dir))
    return {"summary": res.summary.deterministic_dict(), "wall_time_s": res.summary.wall_time_s}


# ---------------------------------------------------------------- commands
def run_verification(
    config_path: str, dataset_ids: list[str] | None = None, *, cross_check_on: bool = True
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, list[DownloadRecord]]:
    """Download (or validate cached copies of) and verify datasets; write evidence JSON."""
    cfg = _load(config_path)
    datasets = [cfg.dataset(d) for d in dataset_ids] if dataset_ids else list(cfg.datasets)
    recs = ensure_downloaded(cfg, datasets)
    with ProcessPoolExecutor(max_workers=min(4, len(datasets) + 1)) as pool:
        futs = [pool.submit(_verify_worker, config_path, d.id) for d in datasets]
        cross = None
        if cross_check_on:
            primaries = [d for d in datasets if d.role == "primary"]
            checks = [d for d in datasets if d.role == "cross_check"]
            if primaries and checks:
                cross = pool.submit(_cross_worker, config_path, primaries[0].id, checks[0].id)
        results = [f.result() for f in futs]
        cross_result = cross.result() if cross is not None else None
    out = cfg.paths.evidence_dir
    write_json(out / "dataset_verification.json", {"datasets": results})
    if cross_result is not None:
        write_json(out / "provider_cross_check.json", cross_result)
    return results, cross_result, recs


def cmd_verify_data(args: argparse.Namespace) -> int:
    console.rule("[bold]DATASET VERIFICATION")
    results, cross_result, recs = run_verification(
        args.config, args.dataset, cross_check_on=args.cross_check
    )
    for r in recs:
        state = "cached " if r.from_cache else "fetched"
        console.print(f"  {state} {r.url}  {r.bytes:,} B  sha256={r.sha256[:16]}…")
    _print_verification(results, cross_result)
    return 0 if all(r["verdict"]["pass"] for r in results) else 1


def _print_verification(results: list[dict[str, Any]], cross: dict[str, Any] | None) -> None:
    t = Table(title="Verified datasets (every record parsed by this tool)")
    for col in (
        "dataset",
        "period",
        "snap",
        "deltas",
        "u != last+1",
        "checkpoint match",
        "trades",
        "verdict",
    ):
        t.add_column(col)
    for r in results:
        b = r["book"]
        t.add_row(
            r["dataset_id"],
            f"{b['first_ts']}\n{b['last_ts']}",
            str(b["snapshots"]),
            f"{b['deltas']:,}",
            str(b.get("update_id_not_plus_one_deltas", "n/a (no IDs)")),
            f"{b['checkpoint_matches']}/{b['checkpoint_comparisons']}",
            f"{r['trades']['rows']:,}",
            "[green]PASS" if r["verdict"]["pass"] else "[red]FAIL",
        )
    console.print(t)
    if cross:
        tr, tob = cross["trades"], cross["top_of_book"]
        console.print(
            f"Cross-provider: {tr['common_trade_ids']:,} common trade IDs, "
            f"{tr['common_ids_with_price_qty_or_side_mismatch']} field mismatches; "
            f"top-of-book identical at {tob['identical_best_bid_and_ask']}/"
            f"{tob['compared_samples']} minute marks."
        )


def cmd_replay(args: argparse.Namespace) -> int:
    overrides = {
        k: v
        for k, v in (
            ("max_events", args.max_events),
            ("speed", args.speed),
            ("start_utc", args.start),
            ("end_utc", args.end),
        )
        if v is not None
    }
    cfg = _load(args.config, overrides)
    out = Path(args.out) if args.out else cfg.paths.evidence_dir
    ensure_downloaded(cfg, [cfg.primary()])
    res = run_replay(cfg, cfg.primary(), out)
    print_replay_report(cfg, res)
    return 0


def cmd_evidence(args: argparse.Namespace) -> int:
    return 0 if generate_evidence(args.config) else 1


def generate_evidence(config_path: str, *, report: bool = True) -> bool:
    """Replay the primary dataset into the evidence dir (in this process) while an
    independent second process replays it into a temp dir; compare SHA-256 of
    every output file. Returns True when the two runs are byte-identical."""
    cfg = _load(config_path)
    ensure_downloaded(cfg, [cfg.primary()])
    out = cfg.paths.evidence_dir
    tmp = Path(tempfile.mkdtemp(prefix="bybit_demo_rerun_"))
    if report:
        console.rule("[bold]REPLAY (real data) + independent re-run for determinism")
    try:
        with ProcessPoolExecutor(max_workers=1) as pool:
            second = pool.submit(_replay_worker, config_path, {}, str(tmp))
            res = run_replay(cfg, cfg.primary(), out)
            r2 = second.result()
        comparison = []
        for name in DETERMINISTIC_FILES:
            h1, h2 = sha256_file(out / name), sha256_file(tmp / name)
            comparison.append(
                {"file": name, "run1_sha256": h1, "run2_sha256": h2, "identical": h1 == h2}
            )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    identical = all(c["identical"] for c in comparison)
    write_json(
        out / "determinism_check.json",
        {
            "method": "Two independent processes replayed the same inputs with the same config; "
            "SHA-256 of every output file compared.",
            "identical": identical,
            "files": comparison,
        },
    )
    write_json(
        out / "run_info.json",
        {
            "note": "Wall-clock values live only here so the other evidence files reproduce.",
            "replay_wall_time_s": [res.summary.wall_time_s, r2["wall_time_s"]],
        },
    )
    write_manifest(out)
    if report:
        print_replay_report(cfg, res)
        console.print(
            f"Determinism: {'[green]IDENTICAL' if identical else '[red]DIFFERENT'} "
            f"({len(comparison)} output files, SHA-256 compared across two processes)"
        )
    return identical


def write_manifest(out: Path) -> None:
    """SHA-256 manifest of every file in the evidence directory."""
    paths = [p for p in out.iterdir() if p.is_file() and p.name != "evidence_manifest.json"]
    write_json(out / "evidence_manifest.json", evidence_manifest(paths, out))


# --------------------------------------------------------------- reporting
def print_replay_report(cfg: DemoConfig, res: ReplayResult, *, max_rows: int = 40) -> None:
    s = res.summary
    ds = cfg.primary()
    ok = (
        s.sequence_gaps == 0
        and s.checkpoint_matches == s.checkpoint_comparisons
        and sum(
            v
            for k, v in s.continuity_issue_counts.items()
            if k not in ("SNAPSHOT_INIT", "SNAPSHOT_RESET")
        )
        == 0
    )
    console.rule("[bold]DATASET")
    console.print(f"Provider: Bybit official archive (REAL data)  [{ds.provider.value}]")
    console.print(f"Symbol:   {ds.symbol} (USDT perpetual)   depth: ob{ds.depth}")
    console.print(f"Period:   {us_to_iso(s.first_ts_us or 0)} -> {us_to_iso(s.last_ts_us or 0)}")
    console.print(f"Events:   {s.events:,} ({s.book_events:,} book, {s.trades:,} trades)")
    console.print(f"Snapshot/delta continuity: {'[green]PASS' if ok else '[red]FAIL'}")
    console.rule("[bold]BOOK RECONSTRUCTION")
    console.print(
        f"Snapshots: {s.snapshots}   Deltas applied: {s.deltas_applied:,}   "
        f"skipped: {s.deltas_skipped}"
    )
    console.print(
        f"SEQUENCE_GAP events (u != last u + 1): {s.sequence_gaps}   "
        f"Crossed books: {s.crossed_books}"
    )
    console.print(
        "Checkpoint snapshots matched by reconstruction: "
        f"{s.checkpoint_matches}/{s.checkpoint_comparisons}"
    )
    console.print(
        f"Final best bid/ask: {s.final_best_bid} / {s.final_best_ask}  "
        f"(levels {s.final_bid_levels}/{s.final_ask_levels})"
    )
    console.rule("[bold]ORDER FLOW")
    console.print(
        f"Trades: {s.trades:,} (buy {s.buy_trades:,} / sell {s.sell_trades:,}; "
        f"aggressor side REPORTED by exchange, inferred: {s.inferred_side_trades})"
    )
    console.print(f"Aggressive buy notional:  {float(s.buy_notional):,.2f} USDT")
    console.print(f"Aggressive sell notional: {float(s.sell_notional):,.2f} USDT")
    console.print(
        f"Session CVD: {float(s.cvd_notional):,.2f} USDT  "
        f"({float(s.cvd_qty):,.2f} {ds.symbol.removesuffix('USDT')})"
    )
    console.rule("[bold]SIGNAL AUDIT")
    console.print(
        f"Evaluations: {s.evaluations:,}   Transitions: {s.transitions:,}   "
        f"CONFIRMED: {s.confirmed}"
    )
    t = Table(title="Transition counts")
    t.add_column("transition")
    t.add_column("count", justify="right")
    for k, v in s.transition_counts.items():
        t.add_row(k, str(v))
    console.print(t)
    if s.rejection_counts:
        console.print(
            "Rejection flags: " + ", ".join(f"{k}={v}" for k, v in s.rejection_counts.items())
        )
    if s.confirmed == 0:
        console.print(f"[yellow]{NO_CONFIRMATION}")
    chains = [r for r in res.memory.transitions if r.new_state.value in ("CANDIDATE", "CONFIRMED")]
    shown = set()
    for rec in chains[:max_rows]:
        if rec.setup_id in shown:
            continue
        shown.add(rec.setup_id)
        for r in (
            x
            for x in res.memory.transitions
            if x.setup_id == rec.setup_id and x.direction == rec.direction
        ):
            f = r.features
            console.print(
                f"[{us_to_iso(r.ts_us)}] {r.direction.value} "
                f"{r.previous_state.value} -> {r.new_state.value} "
                f"setup={r.setup_id} u={r.update_id}\n"
                f"    reasons={list(r.reasons)} flags={[x.value for x in r.rejection_flags]}\n"
                f"    bid/ask={f['best_bid']}/{f['best_ask']} spread_bps={f['spread_bps']:.2f} "
                f"cvd_5s={f['cvd_short_usdt']:.1f} imb_30s={f['imb_medium']:.3f} "
                f"book_imb={f['near_book_imbalance']:.3f} "
                f"ext_up/down_sigma={f['ext_up_sigma']:.2f}/{f['ext_down_sigma']:.2f}"
            )
    console.print(
        "[dim]Research hypotheses only. This output does not claim historical profitability."
    )


# --------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bybit_demo", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--config", default="config/demo.yaml")
    p.add_argument("--log-level", default="WARNING")
    p.add_argument("--json-logs", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    v = sub.add_parser("verify-data", help="download and verify configured datasets")
    v.add_argument("--dataset", action="append", help="dataset id (repeatable); default all")
    v.add_argument("--no-cross-check", dest="cross_check", action="store_false")
    v.set_defaults(func=cmd_verify_data)

    r = sub.add_parser("replay", help="replay the primary dataset and write evidence")
    r.add_argument("--out", help="output directory (default: paths.evidence_dir)")
    r.add_argument("--max-events", type=int)
    r.add_argument("--speed", type=float, help="0 = fast deterministic; 1 = real time")
    r.add_argument("--start", help="ISO-8601 UTC start of the evaluation window")
    r.add_argument("--end", help="ISO-8601 UTC end of the replay")
    r.set_defaults(func=cmd_replay)

    e = sub.add_parser("evidence", help="replay twice, verify determinism, write evidence manifest")
    e.set_defaults(func=cmd_evidence)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level, json_format=args.json_logs)
    try:
        return int(args.func(args))
    except BybitDemoError as exc:
        console.print(f"[red]error:[/red] {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
