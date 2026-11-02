"""Evidence-file helpers: deterministic JSON, hashing and manifests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from bybit_demo.book.continuity import BookSynchronizer
from bybit_demo.config import DemoConfig
from bybit_demo.data.downloader import sha256_file


def write_json(path: Path, obj: Any) -> None:
    """Deterministic JSON with LF line endings on every OS (evidence hashes must not vary)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def config_fingerprint(cfg: DemoConfig) -> str:
    """SHA-256 of the canonical JSON form of the effective configuration."""
    canonical = json.dumps(cfg.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


BYBIT_RULES = [
    "Book initialises only from a 'snapshot' message; a delta before any snapshot is rejected "
    "(DELTA_BEFORE_SNAPSHOT).",
    "A later snapshot replaces the book; before replacing, the reconstructed book is compared "
    "level-by-level with it (checkpoint).",
    "u == last_u -> DUPLICATE (skipped); u < last_u -> STALE_UPDATE (skipped).",
    "u != last_u + 1 -> SEQUENCE_GAP: book invalidated, all deltas skipped until the next "
    "snapshot (RESYNC). Strict +1 contiguity is an OBSERVED property of the archive, enabled by "
    "config, not a documented Bybit guarantee.",
    "seq <= last_seq on a delta -> SEQ_REGRESSION (warning; Bybit documents seq as the "
    "cross sequence where smaller = earlier).",
    "Timestamp moving backwards -> TIMESTAMP_REGRESSION (warning).",
    "Snapshot with u == 1 after prior data -> SERVICE_RESTART (Bybit-documented restart signal).",
    "best_bid >= best_ask after applying an event -> CROSSED_BOOK (signals blocked while crossed).",
]


def continuity_report(
    sync: BookSynchronizer, dataset: dict[str, Any], provider_rules: list[str]
) -> dict[str, Any]:
    st = sync.stats
    failures = [i.to_dict() for i in sync.issues if i.is_failure]
    resets = [i.to_dict() for i in sync.issues if not i.is_failure]
    return {
        "dataset": dataset,
        "provider_rules_applied": provider_rules,
        "status": "PASS"
        if st.failures() == 0 and st.checkpoint_matches == st.checkpoint_comparisons
        else "FAIL",
        "snapshots_applied": st.snapshots,
        "deltas_applied": st.deltas_applied,
        "deltas_skipped": st.deltas_skipped,
        "checkpoint_comparisons": st.checkpoint_comparisons,
        "checkpoint_matches": st.checkpoint_matches,
        "issue_counts": dict(sorted(st.issue_counts.items())),
        "failures_total": st.failures(),
        "failures": failures,
        "snapshot_events": resets,
    }


def evidence_manifest(paths: list[Path], root: Path) -> dict[str, Any]:
    files = []
    for p in sorted(paths):
        if p.exists():
            files.append(
                {
                    "file": p.relative_to(root).as_posix()
                    if p.is_relative_to(root)
                    else p.as_posix(),
                    "bytes": p.stat().st_size,
                    "sha256": sha256_file(p),
                }
            )
    return {"files": files}
