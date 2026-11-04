"""Independent causality check of a clocked replay log. Standard library only.

    python scripts/verify_causal_replay.py best_supported uniform_50ms ...
    python scripts/verify_causal_replay.py --all

Does NOT import ``bybit_demo``. From the clock parameters in the log header and the raw
files, it derives each record's processing-ready time with its own code (including FIFO
within a stream), then for every logged transition at time t rebuilds trade-flow windows
and the order book from records with ready time <= t only, and compares 10 logged
features. A feature that consumed a record before its availability time would differ.

Also checks that no record becomes ready before it happened, and that FIFO adjustment
counts equal the replay's clock_report.json.

Writes data/evidence/causal_independent_verification.json.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import gzip
import hashlib
import io
import json
import sys
import zipfile
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / "data/evidence/causal_logs"
OUT = ROOT / "data/evidence/causal_independent_verification.json"
TARDIS_DS = "tardis_ORDIUSDT_2026-10-01"
TOL = 1e-6


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def rows(path: Path) -> Iterator[dict[str, str]]:
    with gzip.open(path, "rb") as gz, io.TextIOWrapper(gz, encoding="utf-8", newline="") as fh:
        yield from csv.DictReader(fh)


def fifo(times: list[int]) -> tuple[list[int], int]:
    out: list[int] = []
    adjusted = 0
    for t in times:
        if out and t < out[-1]:
            adjusted += 1
            out.append(out[-1])
        else:
            out.append(t)
    return out, adjusted


def read_log(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    header: dict[str, Any] = {}
    transitions: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            if rec.get("record_type") == "run_header":
                header = rec
            elif rec.get("record_type") == "state_transition":
                transitions.append(rec)
    return header, transitions


class Book:
    def __init__(self) -> None:
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.u: int | None = None

    def update(self, side: dict[Decimal, Decimal], price: Decimal, qty: Decimal) -> None:
        if qty == 0:
            side.pop(price, None)
        else:
            side[price] = qty

    def features(self, band_bps: Decimal) -> dict[str, Any]:
        bid, ask = max(self.bids), min(self.asks)
        mid = (bid + ask) / 2
        off = mid * band_bps / 10_000
        return {
            "best_bid": bid,
            "best_ask": ask,
            "depth_bid_usdt": float(sum(p * q for p, q in self.bids.items() if p >= mid - off)),
            "depth_ask_usdt": float(sum(p * q for p, q in self.asks.items() if p <= mid + off)),
        }


def archive_book(path: Path, clock: dict[str, Any]) -> tuple[list[int], list[Any], int, int]:
    msgs: list[Any] = []
    raw: list[int] = []
    negative = 0
    with zipfile.ZipFile(path) as zf, zf.open(zf.namelist()[0]) as fh:
        for line in fh:
            m = json.loads(line)
            t = (m["ts"] * 1000) + clock["book_transport_us"] + clock["processing_us"]
            negative += t < m["cts"] * 1000
            raw.append(t)
            msgs.append(m)
    ready, adjusted = fifo(raw)
    return ready, msgs, adjusted, negative


def tardis_book(path: Path, clock: dict[str, Any]) -> tuple[list[int], list[Any], int, int]:
    msgs: list[Any] = []
    raw: list[int] = []
    negative = 0
    key = None
    for r in rows(path):
        k = (r["timestamp"], r["local_timestamp"], r["is_snapshot"])
        if k != key:
            key = k
            t = int(r["local_timestamp"]) + clock["processing_us"]
            negative += t < int(r["timestamp"])
            raw.append(t)
            msgs.append({"snapshot": r["is_snapshot"] == "true", "rows": []})
        msgs[-1]["rows"].append((r["side"], Decimal(r["price"]), Decimal(r["amount"])))
    ready, adjusted = fifo(raw)
    return ready, msgs, adjusted, negative


def apply_archive(book: Book, m: dict[str, Any]) -> None:
    if m["type"] == "snapshot":
        book.bids.clear()
        book.asks.clear()
    for key, side in (("b", book.bids), ("a", book.asks)):
        for p, q in m["data"][key]:
            book.update(side, Decimal(p), Decimal(q))
    book.u = int(m["data"]["u"])


def apply_tardis(book: Book, m: dict[str, Any]) -> None:
    if m["snapshot"]:
        book.bids.clear()
        book.asks.clear()
    for side, p, q in m["rows"]:
        book.update(book.bids if side == "bid" else book.asks, p, q)


def trade_series(
    header: dict[str, Any], manifest: dict[tuple[str, str], Any]
) -> tuple[list[int], list[Decimal], int, int]:
    clock = header["clock"]
    tardis_only = clock["mode"] == "actual_receipt"
    path = ROOT / header["trades_file"]
    raw: list[int] = []
    signed: list[Decimal] = []
    negative = 0
    receipts: dict[str, int] = {}
    if clock["use_trade_receipts"]:
        receipts = {
            r["id"]: int(r["local_timestamp"])
            for r in rows(ROOT / manifest[(TARDIS_DS, "trades")]["local_path"])
        }
    for r in rows(path):
        if tardis_only:
            filled = int(r["timestamp"])
            t = int(r["local_timestamp"])
            n = Decimal(r["price"]) * Decimal(r["amount"])
            buy = r["side"] == "buy"
        else:
            filled = int(Decimal(r["timestamp"]) * 1_000_000)
            n = Decimal(r["price"]) * Decimal(r["size"])
            buy = r["side"] == "Buy"
            if clock["use_trade_receipts"]:
                t = receipts[r["trdMatchID"]] + clock["trade_transport_us"]
            else:
                t = filled + clock["trade_publication_us"] + clock["trade_transport_us"]
        t += clock["processing_us"]
        negative += t < filled
        raw.append(t)
        signed.append(n if buy else -n)
    ready, adjusted = fifo(raw)
    return ready, signed, adjusted, negative


def verify(name: str, manifest: dict[tuple[str, str], Any]) -> dict[str, Any]:
    d = LOGS / name
    header, transitions = read_log(d / "demo_events.jsonl")
    clock = header["clock"]
    report = json.loads((d / "clock_report.json").read_text(encoding="utf-8"))
    book_path, trades_path = ROOT / header["book_file"], ROOT / header["trades_file"]
    provenance_ok = (
        sha256(book_path) == header["book_file_sha256"]
        and sha256(trades_path) == header["trades_file_sha256"]
    )
    tardis_only = clock["mode"] == "actual_receipt"
    t_ready, signed, t_adj, t_neg = trade_series(header, manifest)
    if tardis_only:
        b_ready, msgs, b_adj, b_neg = tardis_book(book_path, clock)
    else:
        b_ready, msgs, b_adj, b_neg = archive_book(book_path, clock)
    cum_s = [Decimal(0)]
    cum_a = [Decimal(0)]
    for v in signed:
        cum_s.append(cum_s[-1] + v)
        cum_a.append(cum_a[-1] + abs(v))

    def window(t: int, w: int) -> tuple[Decimal, Decimal, int]:
        hi, lo = bisect.bisect_right(t_ready, t), bisect.bisect_right(t_ready, t - w)
        return cum_s[hi] - cum_s[lo], cum_a[hi] - cum_a[lo], hi - lo

    bands = {
        r["thresholds"]["gates.depth_band_bps"]
        for r in transitions
        if "gates.depth_band_bps" in r["thresholds"]
    }
    band = Decimal(str(bands.pop()))
    book = Book()
    i = 0
    mismatches: list[dict[str, Any]] = []
    for rec in transitions:
        t = rec["ts_us"]
        while i < len(msgs) and b_ready[i] <= t:
            (apply_tardis if tardis_only else apply_archive)(book, msgs[i])
            i += 1
        s5, _, n5 = window(t, 5_000_000)
        s30, a30, _ = window(t, 30_000_000)
        ours: dict[str, Any] = {
            "cvd_short_usdt": float(s5),
            "cvd_medium_usdt": float(s30),
            "imb_medium": float(s30 / a30) if a30 else 0.0,
            "trades_short": n5,
            "cvd_session_usdt": float(cum_s[bisect.bisect_right(t_ready, t)]),
            **book.features(band),
        }
        logged = rec["features"]
        diff: dict[str, tuple[Any, Any]] = {}
        for k, v in ours.items():
            if isinstance(v, Decimal):
                bad = v != Decimal(str(logged[k]))
            elif isinstance(v, int):
                bad = v != logged[k]
            else:
                bad = abs(v - float(logged[k])) > TOL * max(1.0, abs(v))
            if bad:
                diff[k] = (str(v), logged[k])
        if not tardis_only and book.u != rec["update_id"]:
            diff["update_id"] = (book.u, rec["update_id"])
        if diff:
            mismatches.append({"timestamp": rec["timestamp"], "diff": diff})
    expected_fifo = (
        report["book_stream"]["fifo_adjusted"],
        report["trade_stream"]["fifo_adjusted"],
    )
    result = {
        "transitions_checked": len(transitions),
        "features_per_transition": 9 if tardis_only else 10,
        "mismatching_transitions": len(mismatches),
        "first_mismatches": mismatches[:5],
        "raw_file_hashes_match_log_header": provenance_ok,
        "records_ready_before_they_happened": b_neg + t_neg,
        "fifo_adjustments_recomputed": [b_adj, t_adj],
        "fifo_adjustments_reported_by_replay": list(expected_fifo),
    }
    result["pass"] = (
        not mismatches and provenance_ok and b_neg + t_neg == 0 and (b_adj, t_adj) == expected_fifo
    )
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenarios", nargs="*")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    names = args.scenarios
    if args.all:
        names = sorted(p.parent.name for p in LOGS.glob("*/clock_report.json"))
    manifest_rows = json.loads(
        (ROOT / "data/evidence/download_manifest.json").read_text(encoding="utf-8")
    )
    manifest = {(m["dataset_id"], m["kind"]): m for m in manifest_rows}
    results = {}
    for name in names:
        results[name] = verify(name, manifest)
        r = results[name]
        print(
            f"{name:32s} transitions {r['transitions_checked']:,}  "
            f"mismatches {r['mismatching_transitions']}  pass {r['pass']}"
        )
    ok = bool(results) and all(r["pass"] for r in results.values())
    OUT.write_text(
        json.dumps(
            {
                "generator": "scripts/verify_causal_replay.py (stdlib only)",
                "scenarios": results,
                "pass": ok,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
