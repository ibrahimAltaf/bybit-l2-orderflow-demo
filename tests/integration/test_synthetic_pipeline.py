"""SYNTHETIC end-to-end tests: fixture files -> real parsers -> replay -> audit writers.

These validate state-machine behaviour. They are NOT evidence about real markets.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bybit_demo.audit.logger import CsvSignalWriter, JsonlAuditWriter
from bybit_demo.data.normalization import iter_bybit_orderbook, load_bybit_trades
from bybit_demo.data.validators import ValidationReport
from bybit_demo.models.signals import Direction, RejectionReason
from bybit_demo.models.signals import SignalState as S
from tests.fixtures.synthetic import scenarios
from tests.helpers import run_events


def _replay_files(sc: scenarios.Scenario, tmp: Path) -> tuple[object, object]:
    book_path, trades_path = scenarios.write_bybit_format(sc, tmp)
    rep_b, rep_t = ValidationReport("b", strict=True), ValidationReport("t", strict=True)
    trades, _ = load_bybit_trades(trades_path, rep_t)
    return run_events([iter_bybit_orderbook(book_path, rep_b), trades])


def chain(sink: object, direction: Direction) -> list[S]:
    return [r.new_state for r in sink.transitions if r.direction is direction]  # type: ignore[attr-defined]


def test_long_breakout_full_chain_through_file_parsers(tmp_path: Path) -> None:
    summary, sink = _replay_files(scenarios.long_breakout(), tmp_path)
    assert chain(sink, Direction.LONG)[:3] == [S.WATCH, S.CANDIDATE, S.CONFIRMED]
    assert S.CONFIRMED not in chain(sink, Direction.SHORT)
    assert summary.sequence_gaps == 0 and summary.crossed_books == 0  # type: ignore[attr-defined]


def test_short_breakdown_mirror(tmp_path: Path) -> None:
    _, sink = _replay_files(scenarios.short_breakdown(), tmp_path)
    assert chain(sink, Direction.SHORT)[:3] == [S.WATCH, S.CANDIDATE, S.CONFIRMED]
    assert S.CONFIRMED not in chain(sink, Direction.LONG)


def test_late_entry_rejected_as_already_extended() -> None:
    sc = scenarios.late_entry()
    _, sink = run_events([sc.book, sc.trades])
    longs = [r for r in sink.transitions if r.direction is Direction.LONG]
    assert S.CONFIRMED not in [r.new_state for r in longs]
    assert any(RejectionReason.ALREADY_EXTENDED in r.rejection_flags for r in longs)


def test_sequence_gap_rejects_active_setup_and_blocks_signals() -> None:
    sc = scenarios.sequence_gap()
    summary, sink = run_events([sc.book, sc.trades])
    assert summary.sequence_gaps == 1
    assert summary.deltas_skipped > 0
    assert any(RejectionReason.SEQUENCE_GAP in r.rejection_flags for r in sink.transitions)
    assert S.CONFIRMED not in [r.new_state for r in sink.transitions]
    assert any(i.kind.value == "SEQUENCE_GAP" for i in sink.issues)


def test_flow_reversal_rejected() -> None:
    sc = scenarios.flow_reversal()
    _, sink = run_events([sc.book, sc.trades])
    longs = [r for r in sink.transitions if r.direction is Direction.LONG]
    assert any(RejectionReason.FLOW_REVERSAL in r.rejection_flags for r in longs)
    assert S.CONFIRMED not in [r.new_state for r in longs]


def test_audit_writers_are_byte_deterministic(tmp_path: Path) -> None:
    sc = scenarios.long_breakout()
    outputs = []
    for run in ("a", "b"):
        jl = JsonlAuditWriter(tmp_path / run / "e.jsonl", {"run": "same"})
        cs = CsvSignalWriter(tmp_path / run / "s.csv")
        _, sink = run_events([sc.book, sc.trades])
        for i in sink.issues:
            jl.on_continuity(i)
        for r in sink.transitions:
            jl.on_transition(r)
            cs.on_transition(r)
        jl.close({"n": len(sink.transitions)})
        cs.close()
        outputs.append(
            ((tmp_path / run / "e.jsonl").read_bytes(), (tmp_path / run / "s.csv").read_bytes())
        )
    assert outputs[0] == outputs[1]
    lines = outputs[0][0].decode().splitlines()
    recs = [json.loads(x) for x in lines]
    assert recs[0]["record_type"] == "run_header" and recs[-1]["record_type"] == "run_footer"
    confirmed = [r for r in recs if r.get("new_state") == "CONFIRMED"]
    assert confirmed and confirmed[0]["signal_timestamp"].startswith("2000-01-01T")


@pytest.mark.parametrize("factory", scenarios.ALL)
def test_synthetic_fixtures_are_marked(factory: object) -> None:
    sc = factory()  # type: ignore[operator]
    assert all(e.symbol == scenarios.SYMBOL for e in [*sc.book, *sc.trades])
    assert all(e.exchange_ts_us < 946_771_200 * 1_000_000 for e in sc.trades)  # all on 2000-01-01
