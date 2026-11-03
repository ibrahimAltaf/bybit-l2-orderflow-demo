"""Determinism verification: same input + same config -> byte-identical analytical output.

    python scripts/verify_determinism.py

Starts two completely separate OS processes, each running

    python -m bybit_demo.cli replay --out <its own temp dir>

on the real ORDIUSDT 2026-10-01 data with config/demo.yaml, then compares the
SHA-256 of the four analytical output files (demo_events.jsonl, signals.csv,
continuity_report.json, replay_summary.json):

  * run A vs. run B                       (repeatability)
  * run A vs. the delivered data/evidence/ (the evidence you received was
                                            produced from the same input/config)

Wall-clock values are never written to these four files (they live only in
run_info.json), so no field has to be excluded from the comparison.

Writes data/evidence/determinism_verification.json; exit code 0 if all identical.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "data" / "evidence"
OUT = EVIDENCE / "determinism_verification.json"
FILES = ("demo_events.jsonl", "signals.csv", "continuity_report.json", "replay_summary.json")


def sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    os.chdir(ROOT)
    dirs = [Path(tempfile.mkdtemp(prefix=f"determinism_{x}_")) for x in "AB"]
    try:
        procs = [
            subprocess.Popen(
                [sys.executable, "-m", "bybit_demo.cli", "replay", "--out", str(d)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            for d in dirs
        ]
        codes = []
        for p in procs:
            _, err = p.communicate()
            codes.append(p.returncode)
            if p.returncode:
                print(err[-2000:])
        rows: list[dict[str, Any]] = []
        for name in FILES:
            a, b, delivered = (
                sha256(dirs[0] / name),
                sha256(dirs[1] / name),
                sha256(EVIDENCE / name),
            )
            rows.append(
                {
                    "file": name,
                    "run_a_sha256": a,
                    "run_b_sha256": b,
                    "delivered_sha256": delivered,
                    "a_equals_b": a is not None and a == b,
                    "a_equals_delivered": a is not None and a == delivered,
                }
            )
    finally:
        for d in dirs:
            shutil.rmtree(d, ignore_errors=True)
    ok = codes == [0, 0] and all(r["a_equals_b"] and r["a_equals_delivered"] for r in rows)
    result = {
        "generator": "scripts/verify_determinism.py",
        "method": "two separate processes: python -m bybit_demo.cli replay --out <tmp>",
        "process_exit_codes": codes,
        "files": rows,
        "pass": ok,
    }
    OUT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", "utf-8", newline="\n")
    for r in rows:
        print(
            f"{r['file']:24s} A==B: {r['a_equals_b']}  A==delivered: {r['a_equals_delivered']}  "
            f"{(r['run_a_sha256'] or '-')[:16]}"
        )
    print(f"determinism PASS={ok} -> data/evidence/determinism_verification.json")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
