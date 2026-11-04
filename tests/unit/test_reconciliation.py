"""Timestamp precision fixtures and signal correspondence, on invented values."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from bybit_demo.audit.signal_compare import (
    CHANGED_SETUP,
    NOT_PRESENT,
    SAME_SETUP,
    Setup,
    compare_signals,
    load_setups,
)
from tests.helpers import ROOT


def script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rec = script("verify_timestamp_reconciliation")


@pytest.mark.parametrize(
    ("text", "us"),
    [
        ("1790812802.071", 1_790_812_802_071_000),
        ("1790812802.1187", 1_790_812_802_118_700),
        ("1790812802.9999", 1_790_812_802_999_900),
        ("1790812802", 1_790_812_802_000_000),
    ],
)
def test_bybit_decimal_seconds_convert_exactly(text: str, us: int) -> None:
    assert rec.bybit_seconds_to_us(text) == us


def test_sub_microsecond_value_is_refused_not_rounded() -> None:
    with pytest.raises(ValueError, match="sub-microsecond"):
        rec.bybit_seconds_to_us("1790812802.1234567")


def test_raw_and_floored_differences_are_reported_separately() -> None:
    cmp = rec.compare_timestamps([(1_000_900, 1_000_000), (2_000_000, 2_000_000)])
    assert cmp["raw_max_abs_diff_us"] == 900
    assert cmp["floored_ms_max_abs_diff_ms"] == 0
    assert cmp["every_tardis_value_is_bybit_floored_to_ms"]


def test_millisecond_boundary_is_not_hidden_by_flooring() -> None:
    cmp = rec.compare_timestamps([(1_001_000, 1_000_999)])
    assert cmp["raw_max_abs_diff_us"] == 1
    assert cmp["floored_ms_max_abs_diff_ms"] == 1  # 1 us apart, but different milliseconds
    assert not cmp["every_tardis_value_is_bybit_floored_to_ms"]


def setup(direction: str, sid: int, start: int, confirmed: int | None, **kw: object) -> Setup:
    s = Setup(direction, sid, start, confirmed or start, entered_us={"WATCH": start})
    if confirmed is not None:
        s.entered_us["CONFIRMED"] = confirmed
    s.flags = list(kw.get("flags", []))  # type: ignore[call-overload]
    return s


SEC = 1_000_000


def test_matching_uses_time_not_list_position_or_setup_id() -> None:
    base = [setup("LONG", 1, 10 * SEC, 12 * SEC), setup("SHORT", 2, 100 * SEC, 102 * SEC)]
    other = [
        setup("SHORT", 900, 100 * SEC, 102 * SEC + 5000),
        setup("LONG", 901, 11 * SEC, 13 * SEC),
    ]
    cmp = compare_signals(base, other)
    assert [r["corrected_signal_id"] for r in cmp["rows"]] == ["LONG-901", "SHORT-900"]
    assert [r["correspondence"] for r in cmp["rows"]] == [SAME_SETUP, SAME_SETUP]
    assert cmp["rows"][1]["delta_ms"] == 5.0


def test_direction_must_match() -> None:
    cmp = compare_signals([setup("LONG", 1, SEC, 2 * SEC)], [setup("SHORT", 2, SEC, 2 * SEC)])
    assert cmp["not_present"] == 1 and cmp["new_count"] == 1


def test_one_corrected_signal_cannot_match_two_baseline_signals() -> None:
    base = [setup("LONG", 1, SEC, 2 * SEC), setup("LONG", 2, 3 * SEC, 4 * SEC)]
    cmp = compare_signals(base, [setup("LONG", 7, 3 * SEC, 4 * SEC)])
    assert cmp["same_setup"] == 1 and cmp["not_present"] == 1


def test_nearby_signal_from_a_different_setup_is_flagged_as_changed() -> None:
    base = [setup("LONG", 1, 10 * SEC, 12 * SEC)]
    cmp = compare_signals(base, [setup("LONG", 5, 20 * SEC, 25 * SEC)])
    assert cmp["rows"][0]["correspondence"] == CHANGED_SETUP


def test_disappearance_reports_what_happened_instead() -> None:
    base = [setup("SHORT", 1, 10 * SEC, 12 * SEC)]
    other = [setup("SHORT", 3, 11 * SEC, None, flags=["FLOW_REVERSAL"])]
    row = compare_signals(base, other)["rows"][0]
    assert row["correspondence"] == NOT_PRESENT
    assert "FLOW_REVERSAL" in row["reason"] and "WATCH" in row["reason"]
    assert "no setup" in compare_signals(base, [])["rows"][0]["reason"]


def test_setups_are_read_from_a_log(tmp_path: Path) -> None:
    def tr(t: int, prev: str, new: str, flags: list[str] | None = None) -> str:
        return json.dumps(
            {
                "record_type": "state_transition",
                "direction": "LONG",
                "setup_id": 4,
                "ts_us": t,
                "update_id": t,
                "previous_state": prev,
                "new_state": new,
                "rejection_flags": flags or [],
            }
        )

    log = tmp_path / "e.jsonl"
    log.write_text(
        "\n".join(
            [
                json.dumps({"record_type": "run_header"}),
                tr(1, "IDLE", "WATCH"),
                tr(2, "WATCH", "CANDIDATE"),
                tr(3, "CANDIDATE", "CONFIRMED"),
                tr(9, "CONFIRMED", "IDLE"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (s,) = load_setups(log)
    assert s.signal_id == "LONG-4" and s.confirmed_us == 3 and s.furthest_state == "CONFIRMED"
    assert s.end_us == 3  # the cooldown is not part of the setup
