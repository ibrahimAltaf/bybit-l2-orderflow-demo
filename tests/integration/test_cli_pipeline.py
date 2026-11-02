"""CLI + pipeline end-to-end on SYNTHETIC files in a temp dir (no network, no real evidence).

Files are placed where the provider registry expects them, so the downloader
treats them as cached and never makes a request.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from bybit_demo.cli import main
from bybit_demo.config import load_config
from bybit_demo.data.provider_registry import FileKind, dataset_files
from bybit_demo.pipeline import run_replay
from tests.fixtures.synthetic import scenarios
from tests.helpers import CONFIG_PATH


@pytest.fixture
def synthetic_config(tmp_path: Path) -> Path:
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    raw["paths"] = {
        "raw_dir": str(tmp_path / "raw"),
        "evidence_dir": str(tmp_path / "evidence"),
        "synthetic_output_dir": str(tmp_path / "synth"),
    }
    raw["datasets"] = [
        {
            "id": "SYNTHETIC_long_breakout",
            "provider": "bybit_archive",
            "symbol": scenarios.SYMBOL,
            "date": "2000-01-01",
            "depth": 200,
            "role": "primary",
            "note": scenarios.MARKER,
        }
    ]
    cfg_path = tmp_path / "synthetic.yaml"
    cfg_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    cfg = load_config(cfg_path)
    book, trades = scenarios.write_bybit_format(scenarios.long_breakout(), tmp_path / "gen")
    targets = {f.kind: f.local_path for f in dataset_files(cfg.primary(), cfg.paths.raw_dir)}
    for kind, src in ((FileKind.BOOK, book), (FileKind.TRADES, trades)):
        targets[kind].parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, targets[kind])
    return cfg_path


def test_run_replay_writes_all_evidence(synthetic_config: Path, tmp_path: Path) -> None:
    cfg = load_config(synthetic_config)
    res = run_replay(cfg, cfg.primary(), tmp_path / "out")
    names = sorted(p.name for p in res.evidence_files)
    assert names == [
        "continuity_report.json",
        "demo_events.jsonl",
        "replay_summary.json",
        "signals.csv",
    ]
    cont = json.loads((tmp_path / "out" / "continuity_report.json").read_text())
    assert cont["status"] == "PASS" and cont["failures_total"] == 0
    lines = (tmp_path / "out" / "demo_events.jsonl").read_text().splitlines()
    assert json.loads(lines[0])["dataset_id"] == "SYNTHETIC_long_breakout"
    assert res.summary.confirmed >= 1
    csv_rows = (tmp_path / "out" / "signals.csv").read_text().splitlines()
    assert csv_rows[0].startswith("timestamp,symbol,direction")
    assert len(csv_rows) == res.summary.transitions + 1


def test_cli_replay_and_evidence(synthetic_config: Path, tmp_path: Path) -> None:
    assert main(["--config", str(synthetic_config), "replay", "--out", str(tmp_path / "r")]) == 0
    assert (tmp_path / "r" / "signals.csv").exists()
    assert main(["--config", str(synthetic_config), "evidence"]) == 0
    ev = tmp_path / "evidence"
    det = json.loads((ev / "determinism_check.json").read_text())
    assert det["identical"] is True and len(det["files"]) == 4
    manifest = json.loads((ev / "evidence_manifest.json").read_text())
    assert {f["file"] for f in manifest["files"]} >= {"demo_events.jsonl", "signals.csv"}


def test_cli_reports_config_errors(tmp_path: Path) -> None:
    assert main(["--config", str(tmp_path / "nope.yaml"), "replay"]) == 2
