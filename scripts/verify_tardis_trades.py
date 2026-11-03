"""Reproduce the Bybit-archive vs. Tardis.dev trade comparison for ORDIUSDT 2026-10-01.

    python scripts/verify_tardis_trades.py

Standard library only; does NOT import the ``bybit_demo`` package. Nothing about
the expected result is hard-coded: every number is computed from the two raw
files, whose exact URLs and SHA-256 come from data/evidence/download_manifest.json.

Sources (both downloaded by ``python -m bybit_demo.cli verify-data``):
  A  Bybit official archive   https://public.bybit.com/trading/ORDIUSDT/ORDIUSDT2026-10-01.csv.gz
     columns: timestamp (Unix seconds, decimal), symbol, side (Buy/Sell), size,
              price, tickDirection, trdMatchID, grossValue, homeNotional,
              foreignNotional, RPI
  B  Tardis.dev free sample   https://datasets.tardis.dev/v1/bybit/trades/2026/10/01/ORDIUSDT.csv.gz
     columns: exchange, symbol, timestamp (Unix microseconds), local_timestamp,
              id, side (buy/sell), price, amount

Matching algorithm
  1. Key: the exchange trade ID (A.trdMatchID == B.id), compared as exact strings.
     No fuzzy matching on time/price is used, so a match cannot be manufactured.
  2. Duplicate IDs inside one source are counted and reported; only the first
     occurrence takes part in matching.
  3. For every common ID, compare:
       price  Decimal(A.price) == Decimal(B.price)   (so "4.620" == "4.62")
       size   Decimal(A.size)  == Decimal(B.amount)
       side   A.side.lower()   == B.side             ("Buy" -> "buy")
       symbol A.symbol         == B.symbol
  4. Timestamps are normalized to integer microseconds:
       A: Decimal(seconds) * 1_000_000 (exact; the file has 0.1 ms precision)
       B: int(microseconds) (Tardis exchange timestamp; observed 1 ms precision)
     Their absolute difference is reported as a distribution. It is not a match
     criterion, because the two sources have different precision.
  5. Order: neither file is assumed sorted; out-of-order rows are counted.

Writes data/evidence/tardis_trade_comparison.json. Exit code 0 when every ID is
present in both sources and no common ID differs in price, size, side or symbol.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import sys
from collections import Counter
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "data" / "evidence"
BYBIT_DS, TARDIS_DS = "bybit_ORDIUSDT_2026-10-01", "tardis_ORDIUSDT_2026-10-01"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def rows(path: Path) -> Iterator[dict[str, str]]:
    with gzip.open(path, "rb") as gz, io.TextIOWrapper(gz, encoding="utf-8", newline="") as fh:
        yield from csv.DictReader(fh)


def load(path: Path, *, id_col: str, ts_us: Any, side: Any, size_col: str) -> dict[str, Any]:
    trades: dict[str, tuple[int, Decimal, Decimal, str, str]] = {}
    dup = out_of_order = n = 0
    prev = None
    for r in rows(path):
        n += 1
        t = ts_us(r["timestamp"])
        if prev is not None and t < prev:
            out_of_order += 1
        prev = t
        key = r[id_col]
        if key in trades:
            dup += 1
            continue
        trades[key] = (t, Decimal(r["price"]), Decimal(r[size_col]), side(r["side"]), r["symbol"])
    return {"rows": n, "duplicates": dup, "out_of_order": out_of_order, "trades": trades}


def pct(values: list[int]) -> dict[str, int]:
    if not values:
        return {}
    s = sorted(values)
    return {f"p{q:g}": s[min(len(s) - 1, int(q / 100 * len(s)))] for q in (0, 50, 99, 100)}


def main() -> int:
    manifest = json.loads((EVIDENCE / "download_manifest.json").read_text(encoding="utf-8"))
    rec = {(m["dataset_id"], m["kind"]): m for m in manifest}
    a_rec, b_rec = rec[(BYBIT_DS, "trades")], rec[(TARDIS_DS, "trades")]
    a_path, b_path = ROOT / a_rec["local_path"], ROOT / b_rec["local_path"]
    for r, p in ((a_rec, a_path), (b_rec, b_path)):
        if not p.exists() or sha256(p) != r["sha256"]:
            print(f"{p}: missing or SHA-256 differs from manifest; run verify-data first")
            return 2

    a = load(
        a_path,
        id_col="trdMatchID",
        ts_us=lambda v: int(Decimal(v) * 1_000_000),
        side=lambda v: v.lower(),
        size_col="size",
    )
    b = load(b_path, id_col="id", ts_us=int, side=lambda v: v, size_col="amount")
    ta, tb = a["trades"], b["trades"]
    common = ta.keys() & tb.keys()
    field_mismatch: Counter[str] = Counter()
    examples: list[dict[str, Any]] = []
    ts_diff_us: list[int] = []
    for k in sorted(common):
        (t1, p1, q1, s1, y1), (t2, p2, q2, s2, y2) = ta[k], tb[k]
        bad = [
            n
            for n, x, y in (
                ("price", p1, p2),
                ("size", q1, q2),
                ("side", s1, s2),
                ("symbol", y1, y2),
            )
            if x != y
        ]
        for n in bad:
            field_mismatch[n] += 1
        if bad and len(examples) < 10:
            examples.append(
                {
                    "id": k,
                    "fields": bad,
                    "bybit": [str(x) for x in ta[k]],
                    "tardis": [str(x) for x in tb[k]],
                }
            )
        ts_diff_us.append(abs(t1 - t2))
    mismatching_ids = sum(1 for k in common if ta[k][1:] != tb[k][1:])
    ok = (
        not (ta.keys() - tb.keys())
        and not (tb.keys() - ta.keys())
        and mismatching_ids == 0
        and len(common) > 0
    )
    result: dict[str, Any] = {
        "generator": "scripts/verify_tardis_trades.py (stdlib only)",
        "symbol": "ORDIUSDT",
        "utc_date": "2026-10-01",
        "source_a": {
            "provider": "Bybit official archive",
            "url": a_rec["url"],
            "sha256": a_rec["sha256"],
            "rows": a["rows"],
            "duplicate_ids": a["duplicates"],
            "out_of_order_rows": a["out_of_order"],
        },
        "source_b": {
            "provider": "Tardis.dev free first-of-month sample",
            "url": b_rec["url"],
            "sha256": b_rec["sha256"],
            "rows": b["rows"],
            "duplicate_ids": b["duplicates"],
            "out_of_order_rows": b["out_of_order"],
        },
        "matching": {
            "key": "exact string equality of Bybit trdMatchID and Tardis id",
            "compared_fields": "price (Decimal), size/amount (Decimal), side (lower-case), symbol",
            "timestamp": "normalized to integer microseconds; reported, not matched",
        },
        "results": {
            "common_trade_ids": len(common),
            "only_in_bybit": len(ta.keys() - tb.keys()),
            "only_in_tardis": len(tb.keys() - ta.keys()),
            "common_ids_with_any_field_mismatch": mismatching_ids,
            "field_mismatch_counts": dict(field_mismatch),
            "mismatch_examples": examples,
            "timestamp_abs_diff_us": pct(ts_diff_us),
            "timestamps_identical": sum(1 for d in ts_diff_us if d == 0),
        },
        "all_trades_match": ok,
    }
    (EVIDENCE / "tardis_trade_comparison.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    res: dict[str, Any] = result["results"]
    print(
        f"Bybit rows {a['rows']:,}, Tardis rows {b['rows']:,}; "
        f"common IDs {res['common_trade_ids']:,}; "
        f"only-in-Bybit {res['only_in_bybit']}, only-in-Tardis {res['only_in_tardis']}; "
        f"field mismatches {res['common_ids_with_any_field_mismatch']}; "
        f"timestamp |diff| max {res['timestamp_abs_diff_us'].get('p100')} us -> "
        "data/evidence/tardis_trade_comparison.json"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
