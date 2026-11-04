"""Run every verification command and record exactly what happened.

    python scripts/run_full_verification.py

Writes data/evidence/verification_commands.txt (the commands, in order) and
data/evidence/verification_results.json (exit code, duration, last output lines, plus a
scenario-log stability check: hashes before vs. after re-running every scenario).
Needs the raw files. Exit code 0 only if every command succeeded.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "data/evidence"
SUMMARY = EVIDENCE / "causal_replay_summary.json"

COMMANDS = [
    ("tests_and_coverage", "python -m pytest --cov --cov-branch -q -p no:cacheprovider"),
    ("ruff", "ruff check src tests scripts"),
    ("ruff_format", "ruff format --check src tests scripts"),
    ("mypy_strict", "mypy"),
    ("l2_integrity", "python scripts/verify_l2_integrity.py"),
    ("tardis_trades", "python scripts/verify_tardis_trades.py"),
    ("timestamp_reconciliation", "python scripts/verify_timestamp_reconciliation.py"),
    ("continuity_classification", "python scripts/verify_continuity.py"),
    ("baseline_and_corrected_replays", "python scripts/run_causal_replay.py --workers 5"),
    ("no_lookahead_baseline", "python scripts/verify_no_lookahead.py"),
    (
        "no_lookahead_best_supported",
        "python scripts/verify_no_lookahead.py --scenario best_supported",
    ),
    ("independent_causal_check", "python scripts/verify_causal_replay.py --all"),
    ("determinism", "python scripts/verify_determinism.py"),
    ("benchmark", "python scripts/benchmark_replay.py"),
]


def log_hashes() -> dict[str, Any]:
    if not SUMMARY.exists():
        return {}
    data = json.loads(SUMMARY.read_text(encoding="utf-8"))
    return {k: v["log_sha256"] for k, v in data["scenarios"].items()}


def main() -> int:
    venv_bin = Path(sys.executable).parent
    before = log_hashes()
    results = []
    for name, cmd in COMMANDS:
        parts = cmd.split()
        tool = sys.executable if parts[0] == "python" else str(venv_bin / parts[0])
        t0 = time.perf_counter()
        p = subprocess.run(
            [tool, *parts[1:]], cwd=ROOT, capture_output=True, text=True, check=False
        )
        lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
        results.append(
            {
                "name": name,
                "command": cmd,
                "exit_code": p.returncode,
                "seconds": round(time.perf_counter() - t0, 1),
                "output_tail": lines[-16:],
            }
        )
        print(f"{name:34s} exit {p.returncode}  {lines[-1][:90] if lines else ''}", flush=True)
    after = log_hashes()
    stable = {k: before.get(k) == after.get(k) for k in after}
    ok = all(r["exit_code"] == 0 for r in results) and all(stable.values())
    (EVIDENCE / "verification_commands.txt").write_text(
        "\n".join(cmd for _, cmd in COMMANDS) + "\n", encoding="utf-8", newline="\n"
    )
    (EVIDENCE / "verification_results.json").write_text(
        json.dumps(
            {
                "python": sys.version.split()[0],
                "platform": sys.platform,
                "scenario_logs_identical_to_previous_run": stable,
                "commands": results,
                "all_passed": ok,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print("all passed:", ok)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
