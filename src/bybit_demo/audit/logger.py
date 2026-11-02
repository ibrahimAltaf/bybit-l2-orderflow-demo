"""Structured logging and audit sinks."""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from types import TracebackType
from typing import Any, Self, TextIO

from bybit_demo.book.continuity import ContinuityIssue
from bybit_demo.models.events import us_to_iso
from bybit_demo.models.signals import TransitionRecord


class JsonFormatter(logging.Formatter):
    """One JSON object per log line (UTC timestamps)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": us_to_iso(int(record.created * 1_000_000)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def setup_logging(level: str = "INFO", *, json_format: bool = False) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        JsonFormatter() if json_format else logging.Formatter("%(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def _dump(obj: dict[str, Any]) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


class JsonlAuditWriter:
    """Writes ``demo_events.jsonl``: header, every continuity issue, every transition, footer.

    Contains no wall-clock values so identical inputs give byte-identical files.
    """

    def __init__(self, path: Path, header: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._fh: TextIO = path.open("w", encoding="utf-8", newline="\n")
        self._fh.write(_dump({"record_type": "run_header", **header}) + "\n")

    def on_transition(self, record: TransitionRecord) -> None:
        self._fh.write(_dump(record.to_dict()) + "\n")

    def on_continuity(self, issue: ContinuityIssue) -> None:
        self._fh.write(_dump(issue.to_dict()) + "\n")

    def close(self, footer: dict[str, Any] | None = None) -> None:
        if footer is not None:
            self._fh.write(_dump({"record_type": "run_footer", **footer}) + "\n")
        self._fh.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, et: type[BaseException] | None, ev: BaseException | None, tb: TracebackType | None
    ) -> None:
        if not self._fh.closed:
            self._fh.close()


SIGNAL_COLUMNS = (
    "timestamp",
    "symbol",
    "direction",
    "setup_id",
    "previous_state",
    "new_state",
    "signal_timestamp",
    "reasons",
    "rejection_flags",
    "update_id",
    "cross_seq",
    "best_bid",
    "best_ask",
    "spread_bps",
    "gate_depth_min_usdt",
    "cvd_short_usdt",
    "cvd_medium_usdt",
    "cvd_session_usdt",
    "imb_short",
    "imb_medium",
    "near_book_imbalance",
    "book_pressure",
    "ext_up_sigma",
    "ext_down_sigma",
    "compression_ratio",
)


class CsvSignalWriter:
    """Writes ``signals.csv``: one compact row per state transition."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("w", encoding="utf-8", newline="")
        self._w = csv.writer(self._fh, lineterminator="\n")
        self._w.writerow(SIGNAL_COLUMNS)

    def on_transition(self, record: TransitionRecord) -> None:
        d = record.to_dict()
        f = record.features
        row = {
            **{
                k: d[k]
                for k in (
                    "timestamp",
                    "symbol",
                    "direction",
                    "setup_id",
                    "previous_state",
                    "new_state",
                    "signal_timestamp",
                    "update_id",
                    "cross_seq",
                )
            },
            "reasons": "|".join(record.reasons),
            "rejection_flags": "|".join(r.value for r in record.rejection_flags),
            **{k: f.get(k) for k in SIGNAL_COLUMNS if k in f},
        }
        self._w.writerow(["" if row.get(c) is None else row[c] for c in SIGNAL_COLUMNS])

    def on_continuity(self, issue: ContinuityIssue) -> None:
        return None

    def close(self) -> None:
        self._fh.close()


class MemorySink:
    """Collects records in memory (tests, console summary)."""

    def __init__(self) -> None:
        self.transitions: list[TransitionRecord] = []
        self.issues: list[ContinuityIssue] = []

    def on_transition(self, record: TransitionRecord) -> None:
        self.transitions.append(record)

    def on_continuity(self, issue: ContinuityIssue) -> None:
        self.issues.append(issue)
