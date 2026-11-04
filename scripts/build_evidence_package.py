"""Build the client evidence package and its ZIP from the committed tree + data/evidence/.

    python scripts/build_evidence_package.py

Output: dist/client-evidence-package/ and dist/bybit-l2-feasibility-evidence.zip
(plus dist/bybit-l2-feasibility-evidence.zip.sha256).

* Source, tests, config, docs and scripts are taken from ``git ls-files`` (so no
  venv, caches, IDE files or untracked scratch files can leak in); the working
  tree must be clean.
* evidence/ is copied from data/evidence/; the complete audit logs go to
  evidence/complete/. evidence/evidence_hashes.sha256 lists every evidence file.
* Raw market data is NOT included (redistribution terms not verified);
  START_HERE.md gives the exact URLs and the SHA-256 each download must match.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
PKG = DIST / "client-evidence-package"
ZIP = DIST / "bybit-l2-feasibility-evidence.zip"
SRC_EVIDENCE = ROOT / "data" / "evidence"
COMPLETE = {"demo_events.jsonl", "signals.csv"}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    if status:
        print("working tree not clean; commit first so the package matches a commit:\n" + status)
        return 1
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()

    shutil.rmtree(PKG, ignore_errors=True)
    PKG.mkdir(parents=True)
    for rel in tracked:
        if rel.startswith("data/evidence/") and rel != "data/evidence/download_manifest.json":
            continue  # evidence goes to evidence/; the manifest stays for hash enforcement
        dest = PKG / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dest)

    ev = PKG / "evidence"
    (ev / "complete").mkdir(parents=True)
    for f in sorted(SRC_EVIDENCE.iterdir()):
        if f.is_file():
            shutil.copy2(f, ev / ("complete" if f.name in COMPLETE else "") / f.name)
    shutil.copytree(SRC_EVIDENCE / "causal_logs", ev / "causal_logs")
    lines = [
        f"{sha256(p)}  {p.relative_to(ev).as_posix()}"
        for p in sorted(ev.rglob("*"))
        if p.is_file() and p.name != "evidence_hashes.sha256"
    ]
    (ev / "evidence_hashes.sha256").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    ZIP.unlink(missing_ok=True)
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(PKG.rglob("*")):
            if p.is_file():
                zf.write(p, (Path(PKG.name) / p.relative_to(PKG)).as_posix())
    digest = sha256(ZIP)
    ZIP.with_suffix(".zip.sha256").write_text(
        f"{digest}  {ZIP.name}\n", encoding="utf-8", newline="\n"
    )
    n = sum(1 for p in PKG.rglob("*") if p.is_file())
    print(f"{n} files from commit {commit[:12]} -> {ZIP} ({ZIP.stat().st_size:,} B)")
    print(f"SHA-256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
