"""Tests on the REAL Bybit ORDIUSDT 2026-10-01 sample (skipped if not downloaded).

Download with:  python -m bybit_demo.cli verify-data
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from pathlib import Path

import pytest

from bybit_demo.data.normalization import iter_bybit_orderbook, load_bybit_trades
from bybit_demo.data.provider_registry import FileKind, dataset_files
from bybit_demo.data.validators import ValidationReport
from bybit_demo.data.verification import make_synchronizer, verify_book
from bybit_demo.models.events import BookEvent, TradeEvent
from tests.helpers import ROOT, demo_config, run_events

CFG = demo_config()
PRIMARY = CFG.primary()
FILES = {f.kind: ROOT / f.local_path for f in dataset_files(PRIMARY, CFG.paths.raw_dir)}

# Marked per test, not skipped at import, so these tests are always collected and a
# run without raw data reports each of them as skipped.
pytestmark = [
    pytest.mark.real_data,
    pytest.mark.skipif(
        not all(p.exists() for p in FILES.values()),
        reason="real sample not downloaded (run: python -m bybit_demo.cli verify-data)",
    ),
]


def _book(limit: int | None = None) -> Iterator[BookEvent]:
    it = iter_bybit_orderbook(
        FILES[FileKind.BOOK], ValidationReport("b", strict=True), symbol=PRIMARY.symbol
    )
    return itertools.islice(it, limit) if limit else it


@pytest.fixture(scope="module")
def trades() -> list[TradeEvent]:
    t, _ = load_bybit_trades(
        FILES[FileKind.TRADES], ValidationReport("t", strict=True), symbol=PRIMARY.symbol
    )
    return t


def test_full_day_reconstruction_matches_closing_snapshot() -> None:
    sync = make_synchronizer(PRIMARY, CFG)
    res = verify_book(_book(), sync, has_update_ids=True)
    assert res["snapshots"] == 2
    assert res["update_id_not_plus_one_deltas"] == 0
    assert res["continuity_failures"] == 0
    assert res["checkpoint_comparisons"] == 1 and res["checkpoint_matches"] == 1
    assert res["max_bid_levels"] <= PRIMARY.depth and res["max_ask_levels"] <= PRIMARY.depth


def test_real_prefix_replay_is_deterministic_and_causal(trades: list[TradeEvent]) -> None:
    n = 30_000
    s1, k1 = run_events([_book(n), trades], symbol=PRIMARY.symbol, max_events=n)
    s2, k2 = run_events([_book(n), trades], symbol=PRIMARY.symbol, max_events=n)
    assert s1.deterministic_dict() == s2.deterministic_dict()
    assert [r.to_dict() for r in k1.transitions] == [r.to_dict() for r in k2.transitions]
    cut = n // 2
    _, k3 = run_events([_book(n), trades], symbol=PRIMARY.symbol, max_events=cut)
    assert [r.to_dict() for r in k3.transitions] == [
        r.to_dict() for r in k1.transitions if r.event_index < cut
    ]


def test_real_trades_have_reported_aggressor(trades: list[TradeEvent]) -> None:
    assert len(trades) > 10_000
    assert all(t.aggressor_source.value == "reported" for t in trades)


def test_raw_files_are_not_tracked_by_git() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "data/raw/*" in gitignore
    assert Path(FILES[FileKind.BOOK]).is_relative_to(ROOT / "data" / "raw")
