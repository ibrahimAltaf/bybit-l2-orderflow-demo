"""Download and verify the configured historical datasets.

Equivalent to ``python -m bybit_demo.cli verify-data``; extra arguments are passed through,
e.g. ``python scripts/verify_dataset.py --dataset bybit_ORDIUSDT_2026-10-01``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)

from bybit_demo.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["verify-data", *sys.argv[1:]]))
