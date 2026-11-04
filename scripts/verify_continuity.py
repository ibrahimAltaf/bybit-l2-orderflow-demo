"""Source-aware continuity classification of every book file in the download manifest.

    python scripts/verify_continuity.py [--quiet-threshold-s 30]

Writes data/evidence/continuity_classification.json. A result of zero suspected
findings is NOT a claim that no event is missing; see docs/ORDERBOOK_CONTINUITY_METHODOLOGY.md.
Exit code 0 unless CONFIRMED_MISSING or SUSPECTED_GAP findings exist.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)

from bybit_demo.audit.evidence import write_json  # noqa: E402
from bybit_demo.book.continuity_audit import FindingClass, audit_stream  # noqa: E402
from bybit_demo.config import ProviderId, load_config  # noqa: E402
from bybit_demo.data.provider_registry import FileKind, dataset_files  # noqa: E402
from bybit_demo.data.validators import ValidationReport  # noqa: E402
from bybit_demo.data.verification import iter_book  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet-threshold-s", type=float, default=30.0)
    args = ap.parse_args()
    cfg = load_config(Path("config/demo.yaml"))
    results = []
    bad = 0
    for ds in cfg.datasets:
        path = next(
            f.local_path for f in dataset_files(ds, cfg.paths.raw_dir) if f.kind is FileKind.BOOK
        )
        has_ids = ds.provider is ProviderId.BYBIT_ARCHIVE
        report = ValidationReport(path.name, strict=True)
        audit = audit_stream(
            iter_book(ds, path, report, "ts"),
            ds.symbol,
            has_update_ids=has_ids,
            quiet_threshold_s=args.quiet_threshold_s,
        )
        counts = audit.counts()
        bad += counts[FindingClass.CONFIRMED_MISSING.value]
        bad += counts[FindingClass.SUSPECTED_GAP.value]
        results.append({"dataset_id": ds.id, "clock": "ts", **audit.to_dict()})
        print(f"{ds.id}: {audit.records:,} records; {counts}")
    write_json(
        Path("data/evidence/continuity_classification.json"),
        {"generator": "scripts/verify_continuity.py", "datasets": results},
    )
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
