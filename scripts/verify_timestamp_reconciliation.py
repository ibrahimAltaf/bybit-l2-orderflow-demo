"""Reconcile the "0.9 ms" and "0 ms" Bybit vs Tardis.dev trade timestamp statements.

    python scripts/verify_timestamp_reconciliation.py

Standard library only. Works directly on the raw files named in
data/evidence/download_manifest.json.

Raw fields
  Bybit trade CSV   ``timestamp``: Unix seconds as a decimal string (up to 4 decimals here)
  Tardis trades CSV ``timestamp``: integer microseconds ("timestamp provided by exchange")
                    ``local_timestamp``: integer microseconds ("message arrival timestamp")

Join: exact trade ID (Bybit ``trdMatchID`` == Tardis ``id``).

Two different numbers are computed on purpose:
  raw difference         |bybit_us - tardis_us|, exact integers, no rounding
  floored-ms difference  |floor(bybit_us / 1000) - floor(tardis_us / 1000)|

Writes data/evidence/timestamp_reconciliation.json.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import sys
from collections import Counter
from collections.abc import Iterable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "data" / "evidence"
BYBIT_DS, TARDIS_DS = "bybit_ORDIUSDT_2026-10-01", "tardis_ORDIUSDT_2026-10-01"


def bybit_seconds_to_us(text: str) -> int:
    """Exact conversion; refuses a value that is not a whole number of microseconds."""
    us = Decimal(text) * 1_000_000
    if us != us.to_integral_value():
        raise ValueError(f"{text!r} has sub-microsecond precision")
    return int(us)


def decimals(text: str) -> int:
    return len(text.split(".", 1)[1]) if "." in text else 0


def floor_ms(us: int) -> int:
    return us // 1000


def compare_timestamps(pairs: Iterable[tuple[int, int]]) -> dict[str, Any]:
    """``pairs`` of (bybit_us, tardis_us) for the same trade ID."""
    raw: Counter[int] = Counter()
    floored_nonzero = 0
    n = 0
    for b, t in pairs:
        n += 1
        raw[b - t] += 1
        floored_nonzero += floor_ms(b) != floor_ms(t)
    return {
        "pairs": n,
        "raw_signed_diff_us_distribution": {str(k): raw[k] for k in sorted(raw)},
        "raw_max_abs_diff_us": max((abs(k) for k in raw), default=None),
        "raw_min_signed_diff_us": min(raw, default=None),
        "raw_max_signed_diff_us": max(raw, default=None),
        "floored_ms_max_abs_diff_ms": 1 if floored_nonzero else 0,
        "pairs_differing_after_flooring_both_to_ms": floored_nonzero,
        "every_tardis_value_is_bybit_floored_to_ms": floored_nonzero == 0
        and all(k >= 0 for k in raw),
    }


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _rows(path: Path) -> Iterator[dict[str, str]]:
    with gzip.open(path, "rb") as gz, io.TextIOWrapper(gz, encoding="utf-8", newline="") as fh:
        yield from csv.DictReader(fh)


def main() -> int:
    manifest = json.loads((EVIDENCE / "download_manifest.json").read_text(encoding="utf-8"))
    rec = {(m["dataset_id"], m["kind"]): m for m in manifest}
    a, b = rec[(BYBIT_DS, "trades")], rec[(TARDIS_DS, "trades")]
    a_path, b_path = ROOT / a["local_path"], ROOT / b["local_path"]
    for r, p in ((a, a_path), (b, b_path)):
        if not p.exists() or _sha256(p) != r["sha256"]:
            print(f"{p}: missing or SHA-256 differs; run verify-data first")
            return 2
    tardis: dict[str, tuple[int, str, str, str]] = {}
    tardis_not_whole_ms = 0
    for row in _rows(b_path):
        t = int(row["timestamp"])
        tardis_not_whole_ms += t % 1000 != 0
        tardis[row["id"]] = (t, row["price"], row["amount"], row["side"])
    digits: Counter[int] = Counter()
    pairs: list[tuple[int, int]] = []
    price_text_differs = size_text_differs = 0
    field_mismatch = 0
    for row in _rows(a_path):
        t_raw, p, q, side = tardis[row["trdMatchID"]]
        digits[decimals(row["timestamp"])] += 1
        pairs.append((bybit_seconds_to_us(row["timestamp"]), t_raw))
        price_text_differs += row["price"] != p
        size_text_differs += row["size"] != q
        field_mismatch += (
            Decimal(row["price"]) != Decimal(p)
            or Decimal(row["size"]) != Decimal(q)
            or row["side"].lower() != side
        )
    cmp = compare_timestamps(pairs)
    result = {
        "generator": "scripts/verify_timestamp_reconciliation.py (stdlib only)",
        "bybit": {
            "url": a["url"],
            "sha256": a["sha256"],
            "field": "timestamp, Unix seconds as decimal text",
            "decimal_places_histogram": {str(k): digits[k] for k in sorted(digits)},
        },
        "tardis": {
            "url": b["url"],
            "sha256": b["sha256"],
            "field": "timestamp, integer microseconds",
            "values_not_a_whole_millisecond": tardis_not_whole_ms,
        },
        "join": "exact trade ID",
        "normalization": {
            "price_size": "Decimal, so 4.620 == 4.62",
            "price_text_differs_but_compared_numerically": price_text_differs,
            "size_text_differs_but_compared_numerically": size_text_differs,
            "side": "Bybit Buy/Sell lower-cased, then equal to Tardis buy/sell",
            "price_size_side_mismatches": field_mismatch,
        },
        "timestamps": cmp,
        "explanation": {
            "0.9 ms": (
                "Exact maximum of |Bybit - Tardis| in integer microseconds, reported by "
                "scripts/verify_tardis_trades.py. Correct."
            ),
            "0 ms": (
                "The earlier provider_cross_check.json field timestamp_abs_diff_ms was computed "
                "as abs(diff_us) // 1000, which floors 0-900 us to 0. It was a truncated "
                "display, not an exact difference. The field is now timestamp_abs_diff_us."
            ),
            "cause_of_the_nonzero_difference": (
                "Tardis values are whole milliseconds and Bybit CSV values carry 0.1 ms "
                "precision. Flooring both to ms makes every pair equal, so the observed "
                "difference is precision, not a disagreement about the trade."
            ),
        },
    }
    (EVIDENCE / "timestamp_reconciliation.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    print(
        f"{cmp['pairs']:,} pairs; raw max {cmp['raw_max_abs_diff_us']} us; floored-to-ms max "
        f"{cmp['floored_ms_max_abs_diff_ms']} ms; Tardis = Bybit floored to ms: "
        f"{cmp['every_tardis_value_is_bybit_floored_to_ms']}; field mismatches {field_mismatch}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
