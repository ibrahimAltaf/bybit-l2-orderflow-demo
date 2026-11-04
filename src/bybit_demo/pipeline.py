"""Composition root: builds the replay graph for one dataset and writes evidence."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml

from bybit_demo import __version__
from bybit_demo.audit.evidence import (
    BYBIT_RULES,
    config_fingerprint,
    continuity_report,
    write_json,
)
from bybit_demo.audit.logger import CsvSignalWriter, JsonlAuditWriter, MemorySink
from bybit_demo.book.continuity import BookSynchronizer
from bybit_demo.config import DatasetConfig, DemoConfig, ProviderId
from bybit_demo.data.downloader import DownloadRecord, ManifestStore, download_all, sha256_file
from bybit_demo.data.provider_registry import FileKind, RemoteFile, dataset_files
from bybit_demo.data.validators import ValidationReport
from bybit_demo.data.verification import iter_book, load_trades, make_synchronizer
from bybit_demo.detector.engine import DetectorEngine
from bybit_demo.features.feature_engine import FeatureEngine
from bybit_demo.models.events import BookEvent, MarketEvent, TradeEvent
from bybit_demo.replay.availability import (
    ClockMode,
    ClockScenario,
    ClockSetup,
    load_trade_receipts,
    measure_collector_latency,
    scenarios_from_config,
)
from bybit_demo.replay.clock import ReplayClock
from bybit_demo.replay.engine import ReplayEngine, ReplaySink, ReplaySummary

TIMESTAMP_RULES = [
    "Source publishes no update IDs: sequence gaps are NOT detectable; only snapshot handling, "
    "timestamp monotonicity and crossed-book checks apply.",
]


def load_clock_scenarios(
    cfg: DemoConfig, path: Path = Path("config/causal_scenarios.yaml")
) -> tuple[list[ClockScenario], dict[str, Any]]:
    """Clock scenarios, with measured delays resolved from the Tardis.dev sample."""
    files = _tardis_files(cfg)
    measured = measure_collector_latency(files[FileKind.BOOK], files[FileKind.TRADES])
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))["scenarios"]
    return scenarios_from_config(raw, measured), measured


def _tardis_files(cfg: DemoConfig) -> dict[FileKind, Path]:
    ds = next(d for d in cfg.datasets if d.provider is ProviderId.TARDIS_FREE)
    return {f.kind: f.local_path for f in dataset_files(ds, cfg.paths.raw_dir)}


def build_clock(
    cfg: DemoConfig, scenario: ClockScenario, measured: dict[str, Any]
) -> ClockSetup | None:
    """``None`` for the baseline, so it takes exactly the original code path."""
    if scenario.mode is ClockMode.CTS_BASELINE:
        return None
    receipts = (
        load_trade_receipts(_tardis_files(cfg)[FileKind.TRADES])
        if scenario.use_trade_receipts
        else None
    )
    return ClockSetup(scenario, receipts, {"collector_latency": measured})


def ensure_downloaded(cfg: DemoConfig, datasets: Iterable[DatasetConfig]) -> list[DownloadRecord]:
    files: list[RemoteFile] = [f for ds in datasets for f in dataset_files(ds, cfg.paths.raw_dir)]
    manifest = ManifestStore(cfg.paths.evidence_dir / "download_manifest.json")
    return asyncio.run(download_all(files, manifest))


def build_engine(
    cfg: DemoConfig,
    ds: DatasetConfig,
    sinks: Iterable[ReplaySink],
    sync: BookSynchronizer | None = None,
) -> ReplayEngine:
    sync = sync or make_synchronizer(ds, cfg)
    return ReplayEngine(
        symbol=ds.symbol,
        synchronizer=sync,
        features=FeatureEngine(cfg.features, cfg.detector.gates),
        detector=DetectorEngine(ds.symbol, cfg.detector),
        clock=ReplayClock(cfg.replay.speed),
        sinks=sinks,
    )


@dataclass(slots=True)
class ReplayResult:
    summary: ReplaySummary
    memory: MemorySink
    sync: BookSynchronizer
    evidence_files: list[Path]
    reports: dict[str, Any]


def open_streams(
    cfg: DemoConfig, ds: DatasetConfig
) -> tuple[list[Iterable[MarketEvent]], dict[str, Any]]:
    files = {f.kind: f for f in dataset_files(ds, cfg.paths.raw_dir)}
    # Strict: a replay aborts on the first malformed record instead of skipping it
    # (verify-data counts malformed records and fails its verdict instead).
    book_report = ValidationReport(files[FileKind.BOOK].local_path.name, strict=True)
    trade_report = ValidationReport(files[FileKind.TRADES].local_path.name, strict=True)
    trades, info = load_trades(ds, files[FileKind.TRADES].local_path, trade_report)
    book = iter_book(ds, files[FileKind.BOOK].local_path, book_report, cfg.replay.book_clock)
    meta = {
        "book_file": files[FileKind.BOOK].local_path.as_posix(),
        "trades_file": files[FileKind.TRADES].local_path.as_posix(),
        "book_validation": book_report,
        "trades_validation": trade_report,
        "trades_file_info": info,
    }
    return [book, trades], meta


def run_replay(
    cfg: DemoConfig, ds: DatasetConfig, out_dir: Path, clock: ClockSetup | None = None
) -> ReplayResult:
    """Replay ``ds`` and write demo_events.jsonl, signals.csv, continuity_report.json,
    replay_summary.json into ``out_dir``. Output contains no wall-clock values.

    ``clock=None`` is the original cts baseline, byte for byte. Otherwise every
    event is stamped by the clock scenario before it can reach the replay.
    """
    streams, meta = open_streams(cfg, ds)
    if clock is not None:
        book_stream, trade_stream = streams
        streams = [
            clock.stamp_book(cast("Iterable[BookEvent]", book_stream)),
            clock.stamp_trades(cast("Iterable[TradeEvent]", trade_stream)),
        ]
    header = {
        "code_version": __version__,
        "dataset_id": ds.id,
        "provider": ds.provider.value,
        "symbol": ds.symbol,
        "date": ds.date.isoformat(),
        "book_file": meta["book_file"],
        "book_file_sha256": sha256_file(Path(meta["book_file"])),
        "trades_file": meta["trades_file"],
        "trades_file_sha256": sha256_file(Path(meta["trades_file"])),
        "evaluation_window_utc": [cfg.replay.start_utc, cfg.replay.end_utc],
        "max_events": cfg.replay.max_events,
        "config_sha256": config_fingerprint(cfg),
        "book_clock": cfg.replay.book_clock,
        "data_kind": "REAL historical market data (not synthetic)",
        "disclaimer": "Signal research demonstration only. No profitability claim.",
    }
    if clock is not None:
        header["clock"] = clock.provenance()
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / "demo_events.jsonl"
    csv_path = out_dir / "signals.csv"
    memory = MemorySink()
    jsonl = JsonlAuditWriter(jsonl_path, header)
    csv_w = CsvSignalWriter(csv_path)
    engine = build_engine(cfg, ds, [memory, jsonl, csv_w])
    try:
        summary = engine.run(
            *streams,
            max_events=cfg.replay.max_events,
            start_utc=cfg.replay.start_utc,
            end_utc=cfg.replay.end_utc,
        )
    finally:
        csv_w.close()
    jsonl.close(footer=summary.deterministic_dict())

    rules = BYBIT_RULES if ds.provider is ProviderId.BYBIT_ARCHIVE else TIMESTAMP_RULES
    cont = continuity_report(engine.sync, header, rules)
    cont_path = out_dir / "continuity_report.json"
    write_json(cont_path, cont)
    summary_path = out_dir / "replay_summary.json"
    reports = {
        "book_schema_validation": meta["book_validation"].to_dict(),
        "trades_schema_validation": meta["trades_validation"].to_dict(),
    }
    write_json(summary_path, {"header": header, "summary": summary.deterministic_dict(), **reports})
    if clock is not None:
        write_json(out_dir / "clock_report.json", clock.report())
    return ReplayResult(
        summary, memory, engine.sync, [jsonl_path, csv_path, cont_path, summary_path], reports
    )
