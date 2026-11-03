"""Compare evidence you regenerated (data/evidence/) with the delivered evidence package.

    python scripts/compare_evidence.py --delivered evidence

Run it from the root of the client evidence package after the reproduction
commands in START_HERE.md. For every delivered evidence file it computes the
SHA-256 of the delivered copy and of the regenerated copy in data/evidence/
(complete logs live in <delivered>/complete/ in the package) and reports equality.

run_info.json holds wall-clock timings and is expected to differ; it is reported
but not counted. Exit code 0 if every other file is byte-identical.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGENERATED = ROOT / "data" / "evidence"
EXPECTED_TO_DIFFER = {"run_info.json", "evidence_manifest.json"}
NOT_EVIDENCE = {"evidence_hashes.sha256"}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--delivered", default="evidence", help="delivered evidence directory")
    args = ap.parse_args()
    delivered = (ROOT / args.delivered).resolve()
    if not delivered.is_dir():
        print(f"{delivered} not found")
        return 2
    failures = 0
    for path in sorted(p for p in delivered.rglob("*") if p.is_file()):
        if path.name in NOT_EVIDENCE:
            continue
        mine = REGENERATED / path.name
        if not mine.exists():
            status = "NOT REGENERATED"
            failures += path.name not in EXPECTED_TO_DIFFER
        elif sha256(mine) == sha256(path):
            status = "IDENTICAL"
        elif path.name in EXPECTED_TO_DIFFER:
            status = "differs (expected: wall-clock / hash list)"
        else:
            status = "DIFFERENT"
            failures += 1
        print(f"{path.relative_to(delivered).as_posix():40s} {status}")
    print(f"files that should match but do not: {failures}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
