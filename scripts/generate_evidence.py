"""Replay the primary real dataset, prove determinism, and write the evidence manifest.

Equivalent to ``python -m bybit_demo.cli evidence``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)

from bybit_demo.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["evidence", *sys.argv[1:]]))
