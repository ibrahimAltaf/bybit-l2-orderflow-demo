"""Reproduce the test counts of a delivered ZIP under given interpreters.

    python scripts/reconcile_tests.py --zip <delivered.zip> --python <exe> [--python <exe>]

For each interpreter, in a fresh temporary directory and fresh venv:
  1. extract the ZIP (it contains no raw market data)
  2. plain ``pip install -e ".[dev]"``, recording whether it is accepted; if the
     package metadata refuses the interpreter, retry with ``--ignore-requires-python``
     and say so
  3. ``pytest --collect-only -q`` and ``pytest -q -rs`` with no raw data
  4. copy data/raw from this repository into the tree, then repeat step 3

Writes data/evidence/python_test_reconciliation.json. Nothing here changes test code.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/evidence/python_test_reconciliation.json"
PKG = "client-evidence-package"


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)


def pytest_counts(py: Path, tree: Path) -> dict[str, Any]:
    collect = run([str(py), "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"], tree)
    full = run([str(py), "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider"], tree)
    tail = [ln for ln in full.stdout.splitlines() if ln.strip()][-1:]
    collect_tail = [ln for ln in collect.stdout.splitlines() if ln.strip()][-1:]
    return {
        "command": "python -m pytest -q -rs",
        "collected_tests_line": collect_tail[0] if collect_tail else "",
        "test_ids_collected": sum("::" in ln for ln in collect.stdout.splitlines()),
        "summary_line": tail[0] if tail else full.stderr[-300:],
        "skip_reasons": [ln for ln in full.stdout.splitlines() if ln.startswith("SKIPPED")],
        "real_data_tests_collected": sum(
            "test_real_sample" in ln and "::" in ln for ln in collect.stdout.splitlines()
        ),
        "exit_code": full.returncode,
    }


def one(python: str, delivered: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="reconcile_") as tmp:
        base = Path(tmp)
        with zipfile.ZipFile(delivered) as zf:
            zf.extractall(base)
        tree = base / PKG
        venv = base / "venv"
        made = run([python, "-m", "venv", str(venv)], base)
        if made.returncode:
            return {"interpreter": python, "error": made.stderr[-400:]}
        py = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        plain = run([str(py), "-m", "pip", "install", "-q", "-e", ".[dev]"], tree)
        forced = None
        if plain.returncode:
            forced = run(
                [str(py), "-m", "pip", "install", "-q", "--ignore-requires-python", "-e", ".[dev]"],
                tree,
            )
        version = run([str(py), "-VV"], tree).stdout.strip()
        freeze = run([str(py), "-m", "pip", "freeze", "--exclude-editable"], tree).stdout
        no_data = pytest_counts(py, tree)
        for sub in ("bybit_archive", "tardis_free"):
            shutil.copytree(ROOT / "data/raw" / sub, tree / "data/raw" / sub)
        with_data = pytest_counts(py, tree)
    refused = re.findall(r"ERROR: Package .*", plain.stderr)
    return {
        "interpreter": version,
        "plain_pip_install_exit_code": plain.returncode,
        "plain_pip_install_message": refused[0] if refused else "",
        "installed_with_ignore_requires_python": forced is not None,
        "forced_install_exit_code": None if forced is None else forced.returncode,
        "dependency_versions": freeze.splitlines(),
        "without_raw_data": no_data,
        "with_raw_data": with_data,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", required=True, type=Path)
    ap.add_argument("--python", action="append", required=True)
    args = ap.parse_args()
    results = [one(p, args.zip) for p in args.python]
    OUT.write_text(
        json.dumps({"zip": args.zip.name, "platform": sys.platform, "runs": results}, indent=2)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    for r in results:
        print(r.get("interpreter"), r.get("error", ""))
        if "without_raw_data" in r:
            print(
                "  plain install exit",
                r["plain_pip_install_exit_code"],
                r["plain_pip_install_message"],
            )
            print("  no data  :", r["without_raw_data"]["summary_line"])
            print("  with data:", r["with_raw_data"]["summary_line"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
