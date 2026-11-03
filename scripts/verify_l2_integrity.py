"""Raw snapshot / delta integrity report for every Bybit L2 dataset in the download manifest.

    python scripts/verify_l2_integrity.py

Standard library only; does NOT import the ``bybit_demo`` package, so it is an
independent second implementation of the checks run by ``verify-data``.

Inputs: data/evidence/download_manifest.json (exact URLs, sizes, SHA-256 of the
files actually downloaded) and the raw files under data/raw/ (re-download them
with ``python -m bybit_demo.cli verify-data``).

Outputs (data/evidence/):
  data_references.json   exact original URLs, filenames, sizes, SHA-256, retrieval time
  DATA_REFERENCES.md     the same as a human-readable table
  l2_integrity_report.json  per-dataset raw integrity measurements and checks

Exit code 0 only if every file's hash matches the manifest and every check passes.

Terminology used in the output (deliberately precise):
  * "update_id_not_plus_one" counts deltas whose ``u`` is not exactly previous
    ``u`` + 1. Bybit documents ``u`` as an update ID (``u == 1`` = service
    restart) but does NOT document +1 increments. A count of 0 means "+1 on
    every delta in this file" (OBSERVED), not "Bybit guarantees no gaps".
  * "largest_quiet_intervals" are the longest periods without any book message.
    The archive has no heartbeat, so a quiet interval is not by itself evidence
    of missing data, and missing data inside a quiet interval could not be
    detected by timestamps alone; the ``u`` sequence is the only gap detector.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import sys
import zipfile
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "data" / "evidence"
MANIFEST = EVIDENCE / "download_manifest.json"
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
QUIET_THRESHOLDS_S = (5, 30, 60)


def iso(ms: int) -> str:
    return (EPOCH + timedelta(milliseconds=ms)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def day_bounds_ms(day: str) -> tuple[int, int]:
    d = datetime.fromisoformat(day).replace(tzinfo=UTC)
    lo = (d - EPOCH) // timedelta(milliseconds=1)
    return lo, lo + 86_400_000


def percentiles(values: list[int], qs: tuple[float, ...]) -> dict[str, int]:
    if not values:
        return {}
    s = sorted(values)
    out = {f"p{q * 100:g}": s[min(len(s) - 1, int(q * len(s)))] for q in qs}
    out["max"] = s[-1]
    return out


class Side:
    """One book side with lazily maintained best price."""

    def __init__(self, bid: bool) -> None:
        self.bid = bid
        self.levels: dict[Decimal, Decimal] = {}
        self.best: Decimal | None = None

    def better(self, a: Decimal, b: Decimal) -> bool:
        return a > b if self.bid else a < b

    def clear(self) -> None:
        self.levels.clear()
        self.best = None

    def set(self, price: Decimal, qty: Decimal) -> bool:
        """Apply one level; return False if a delete targeted an absent level."""
        if qty == 0:
            if price not in self.levels:
                return False
            del self.levels[price]
            if price == self.best:
                pick = max if self.bid else min
                self.best = pick(self.levels) if self.levels else None
            return True
        self.levels[price] = qty
        if self.best is None or self.better(price, self.best):
            self.best = price
        return True

    def ordered(self) -> list[tuple[Decimal, Decimal]]:
        return sorted(self.levels.items(), reverse=self.bid)


def parse_levels(raw: Any) -> list[tuple[Decimal, Decimal]]:
    if type(raw) is not list:
        raise ValueError("levels not a list")
    out = []
    seen: set[Decimal] = set()
    for lvl in raw:
        if type(lvl) is not list or len(lvl) != 2:
            raise ValueError("level is not [price, qty]")
        p, q = Decimal(lvl[0]), Decimal(lvl[1])
        if not p.is_finite() or not q.is_finite() or p <= 0 or q < 0:
            raise ValueError(f"invalid level {lvl}")
        if p in seen:
            raise ValueError(f"duplicate price {lvl[0]} in one message")
        seen.add(p)
        out.append((p, q))
    return out


def check_book(path: Path, symbol: str, day: str, depth: int) -> dict[str, Any]:
    lo, hi = day_bounds_ms(day)
    bids, asks = Side(True), Side(False)
    types: Counter[str] = Counter()
    u_diff: Counter[str] = Counter()
    malformed: list[str] = []
    snapshots: list[dict[str, Any]] = []
    gaps: list[tuple[int, int]] = []  # (gap_ms, at_ts)
    lat: list[int] = []
    c: Counter[str] = Counter()
    prev: dict[str, int] | None = None
    prev_raw: bytes | None = None
    max_bid = max_ask = 0
    first_ts: int | None = None
    last_ts: int | None = None
    with zipfile.ZipFile(path) as zf:
        members = zf.namelist()
        with zf.open(members[0]) as fh:
            for line_no, raw in enumerate(fh, start=1):
                c["records"] += 1
                if raw == prev_raw:
                    c["identical_consecutive_records"] += 1
                prev_raw = raw
                try:
                    m = json.loads(raw)
                    typ, ts, cts, data = m["type"], m["ts"], m["cts"], m["data"]
                    u, seq, sym = data["u"], data["seq"], data["s"]
                    if typ not in ("snapshot", "delta"):
                        raise ValueError(f"type {typ!r}")
                    if not all(type(x) is int for x in (ts, cts, u, seq)):
                        raise ValueError("ts/cts/u/seq not integers")
                    if sym != symbol:
                        raise ValueError(f"symbol {sym!r}")
                    b, a = parse_levels(data["b"]), parse_levels(data["a"])
                except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
                    c["malformed_records"] += 1
                    if len(malformed) < 10:
                        malformed.append(f"line {line_no}: {exc}")
                    continue
                types[typ] += 1
                first_ts = ts if first_ts is None else first_ts
                last_ts = ts
                lat.append(ts - cts)
                if cts > ts:
                    c["cts_after_ts"] += 1
                if prev is not None:
                    if ts < prev["ts"]:
                        c["ts_regressions"] += 1
                    if cts < prev["cts"]:
                        c["cts_regressions"] += 1
                    if seq <= prev["seq"] and typ == "delta":
                        c["seq_not_increasing_between_deltas"] += 1
                    gaps.append((ts - prev["ts"], prev["ts"]))
                if typ == "snapshot":
                    rebuilt_b, rebuilt_a = bids.ordered(), asks.ordered()
                    entry: dict[str, Any] = {
                        "line": line_no,
                        "position": "first record" if line_no == 1 else "inside file",
                        "ts": iso(ts),
                        "cts": iso(cts),
                        "u": u,
                        "seq": seq,
                        "bid_levels": len(b),
                        "ask_levels": len(a),
                        "zero_quantity_levels": sum(1 for _, q in b + a if q == 0),
                    }
                    if prev is not None:
                        entry["u_minus_previous_record_u"] = u - prev["u"]
                        entry["seq_minus_previous_record_seq"] = seq - prev["seq"]
                        # Compare the snapshot's published window, best price first.
                        mismatched = 0
                        for ours, theirs in ((rebuilt_b[: len(b)], b), (rebuilt_a[: len(a)], a)):
                            mismatched += sum(
                                1 for x, y in zip(ours, theirs, strict=False) if x != y
                            )
                            mismatched += len(theirs) - len(ours)
                        entry["mismatched_levels"] = mismatched
                        entry["rebuilt_book_equals_this_snapshot"] = mismatched == 0
                    snapshots.append(entry)
                    bids.clear()
                    asks.clear()
                else:
                    if not snapshots:
                        c["deltas_before_first_snapshot"] += 1
                    if prev is not None:
                        d = u - prev["u"]
                        u_diff["+1" if d == 1 else ("0 (duplicate)" if d == 0 else str(d))] += 1
                        if d != 1:
                            c["update_id_not_plus_one"] += 1
                for p, q in b:
                    if not bids.set(p, q):
                        c["deletes_of_absent_levels"] += 1
                for p, q in a:
                    if not asks.set(p, q):
                        c["deletes_of_absent_levels"] += 1
                if bids.best is None or asks.best is None:
                    c["records_leaving_an_empty_side"] += 1
                elif bids.best >= asks.best:
                    c["records_leaving_book_crossed_or_locked"] += 1
                max_bid, max_ask = max(max_bid, len(bids.levels)), max(max_ask, len(asks.levels))
                prev = {"ts": ts, "cts": cts, "u": u, "seq": seq}
    gaps.sort(reverse=True)
    eof = snapshots[-1] if len(snapshots) > 1 else None
    checks = {
        "no_malformed_records": c["malformed_records"] == 0,
        "first_record_is_snapshot": bool(snapshots) and snapshots[0]["line"] == 1,
        "no_delta_before_first_snapshot": c["deltas_before_first_snapshot"] == 0,
        "update_id_plus_one_on_every_delta": c["update_id_not_plus_one"] == 0,
        "seq_strictly_increasing_between_deltas": c["seq_not_increasing_between_deltas"] == 0,
        "no_ts_regression": c["ts_regressions"] == 0,
        "no_cts_regression": c["cts_regressions"] == 0,
        "no_identical_duplicate_records": c["identical_consecutive_records"] == 0,
        "never_crossed_or_locked": c["records_leaving_book_crossed_or_locked"] == 0,
        "depth_never_exceeds_advertised": max_bid <= depth and max_ask <= depth,
        "end_of_file_snapshot_reproduced": bool(eof and eof["rebuilt_book_equals_this_snapshot"]),
        "book_records_within_utc_day_plus_minus_60s": (
            first_ts is not None
            and last_ts is not None
            and first_ts >= lo - 60_000
            and last_ts <= hi + 60_000
        ),
    }
    return {
        "file": path.name,
        "archive_member": members[0],
        "records_total": c["records"],
        "records_by_type": dict(types),
        "first_record_ts": iso(first_ts) if first_ts else None,
        "last_record_ts": iso(last_ts) if last_ts else None,
        "snapshots": snapshots,
        "update_id_increment_distribution_over_deltas": dict(u_diff),
        "counts": {
            k: c[k]
            for k in (
                "malformed_records",
                "deltas_before_first_snapshot",
                "update_id_not_plus_one",
                "seq_not_increasing_between_deltas",
                "ts_regressions",
                "cts_regressions",
                "cts_after_ts",
                "identical_consecutive_records",
                "deletes_of_absent_levels",
                "records_leaving_an_empty_side",
                "records_leaving_book_crossed_or_locked",
            )
        },
        "malformed_examples": malformed,
        "publish_latency_ts_minus_cts_ms": percentiles(lat, (0.0, 0.5, 0.99)),
        "inter_record_gap_ts_ms": percentiles([g for g, _ in gaps], (0.0, 0.001, 0.5, 0.99)),
        "quiet_intervals_longer_than_s": {
            str(s): sum(1 for g, _ in gaps if g > s * 1000) for s in QUIET_THRESHOLDS_S
        },
        "largest_quiet_intervals": [
            {"from": iso(t), "to": iso(t + g), "seconds": g / 1000} for g, t in gaps[:5]
        ],
        "max_levels_held": {"bid": max_bid, "ask": max_ask, "advertised_depth": depth},
        "final_reconstructed_book": {
            "bid_levels": len(bids.levels),
            "ask_levels": len(asks.levels),
            "best_bid": str(bids.best),
            "best_ask": str(asks.best),
        },
        "checks": checks,
    }


def check_trades(path: Path, symbol: str, day: str) -> dict[str, Any]:
    lo, hi = day_bounds_ms(day)
    c: Counter[str] = Counter()
    sides: Counter[str] = Counter()
    ids: set[str] = set()
    prev_t: Decimal | None = None
    first = last = None
    with gzip.open(path, "rb") as gz, io.TextIOWrapper(gz, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames
        for row in reader:
            c["rows"] += 1
            try:
                t = Decimal(row["timestamp"])
                if Decimal(row["price"]) <= 0 or Decimal(row["size"]) <= 0:
                    raise ValueError("non-positive price/size")
                if row["symbol"] != symbol:
                    raise ValueError("symbol")
            except (InvalidOperation, ValueError, KeyError):
                c["malformed_rows"] += 1
                continue
            sides[row["side"]] += 1
            tid = row.get("trdMatchID") or ""
            if tid in ids:
                c["duplicate_trade_ids"] += 1
            ids.add(tid)
            if prev_t is not None and t < prev_t:
                c["out_of_order_rows"] += 1
            prev_t = t
            ms = int(t * 1000)
            first = ms if first is None else first
            last = ms
            if not lo <= ms < hi:
                c["rows_outside_utc_day"] += 1
            c["rpi_rows"] += row.get("RPI") == "1"
    return {
        "file": path.name,
        "columns": header,
        "rows": c["rows"],
        "first_trade": iso(first) if first else None,
        "last_trade": iso(last) if last else None,
        "side_values": dict(sides),
        "counts": {
            k: c[k]
            for k in (
                "malformed_rows",
                "duplicate_trade_ids",
                "out_of_order_rows",
                "rows_outside_utc_day",
                "rpi_rows",
            )
        },
        "checks": {
            "no_malformed_rows": c["malformed_rows"] == 0,
            "no_duplicate_trade_ids": c["duplicate_trade_ids"] == 0,
            "chronological": c["out_of_order_rows"] == 0,
            "all_rows_inside_utc_day": c["rows_outside_utc_day"] == 0,
            "only_buy_sell_side_values": set(sides) <= {"Buy", "Sell"},
        },
    }


SEMANTICS = {
    "bybit_documented": [
        "type is 'snapshot' or 'delta'; a new snapshot means: reset the local book.",
        "u is the 'update ID'; u == 1 on a snapshot signals a service restart.",
        "seq is the cross sequence: a smaller seq was generated earlier.",
        "ts = system generation time (ms); cts = matching-engine time (ms).",
        "quantity '0' deletes the price level.",
        "Linear level-200 data is pushed every 100 ms; RPI orders are not included.",
        "NOT documented: that u increases by exactly 1 per delta.",
        "NOT documented: the archive file layout or who writes the last snapshot.",
        "OBSERVED here: the end-of-file snapshot repeats the last delta's u; in the two "
        "10000LADYSUSDT files it also repeats its seq, in ORDIUSDT its seq is larger. It "
        "therefore restates the book after the last delta rather than adding an update.",
    ],
    "observed_in_these_files": "see per-dataset 'counts', 'snapshots' and 'checks'",
    "rules_enforced_by_bybit_demo": [
        "delta before any snapshot -> rejected (DELTA_BEFORE_SNAPSHOT)",
        "u == previous u -> DUPLICATE, skipped; u < previous u -> STALE_UPDATE, skipped",
        "u != previous u + 1 -> SEQUENCE_GAP: book invalidated until the next snapshot "
        "(our rule, configurable; fails safe if the live stream legitimately skips IDs)",
        "seq not increasing / ts going backwards -> warning, logged",
        "best bid >= best ask -> CROSSED_BOOK, signals blocked",
        "snapshot received while in sync -> compared level by level before reset",
    ],
    "meaning_of_zero_gaps_wording": (
        "Earlier wording 'zero gaps' is replaced by 'update_id_not_plus_one = 0': every delta "
        "in the file had u exactly one greater than the previous record's u. This is an "
        "observation about these files under our strict rule, not a Bybit guarantee, and it "
        "says nothing about files that were not checked or about the live WebSocket."
    ),
}


def main() -> int:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    by_ds: dict[str, dict[str, dict[str, Any]]] = {}
    for rec in manifest:
        if rec["dataset_id"].startswith("bybit_"):
            by_ds.setdefault(rec["dataset_id"], {})[rec["kind"]] = rec
    refs: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    all_ok = True
    for ds_id in sorted(by_ds):
        book, trades = by_ds[ds_id]["book"], by_ds[ds_id]["trades"]
        _, symbol, day = ds_id.split("_", 2)
        depth = int(book["url"].rsplit("_ob", 1)[1].split(".")[0])
        files_ok = True
        for rec in (book, trades):
            p = ROOT / rec["local_path"]
            rec["_present"] = p.exists()
            rec["_sha_ok"] = p.exists() and sha256(p) == rec["sha256"]
            rec["_size_ok"] = p.exists() and p.stat().st_size == rec["bytes"]
            files_ok &= rec["_sha_ok"] and rec["_size_ok"]
        refs.append(
            {
                "dataset_id": ds_id,
                "symbol": symbol,
                "utc_date": day,
                "book": {
                    "url": book["url"],
                    "filename": Path(book["url"]).name,
                    "bytes": book["bytes"],
                    "sha256": book["sha256"],
                    "retrieved_at_utc": book["retrieved_at_utc"],
                    "provider_last_modified": book["last_modified"],
                },
                "trades": {
                    "url": trades["url"],
                    "filename": Path(trades["url"]).name,
                    "bytes": trades["bytes"],
                    "sha256": trades["sha256"],
                    "retrieved_at_utc": trades["retrieved_at_utc"],
                    "provider_last_modified": trades["last_modified"],
                },
            }
        )
        if not files_ok:
            print(f"{ds_id}: raw files missing or hash/size mismatch; run verify-data first")
            all_ok = False
            continue
        b = check_book(ROOT / book["local_path"], symbol, day, depth)
        t = check_trades(ROOT / trades["local_path"], symbol, day)
        ok = all(b["checks"].values()) and all(t["checks"].values())
        all_ok &= ok
        reports.append(
            {
                "dataset_id": ds_id,
                "symbol": symbol,
                "utc_date": day,
                "raw_files_match_manifest_sha256_and_size": True,
                "book": b,
                "trades": t,
                "all_checks_pass": ok,
            }
        )
        print(
            f"{ds_id}: {b['records_total']:,} records ({b['records_by_type']}), "
            f"u!=+1: {b['counts']['update_id_not_plus_one']}, "
            f"end-of-file snapshot reproduced: {b['checks']['end_of_file_snapshot_reproduced']}, "
            f"trades {t['rows']:,}; all checks pass: {ok}"
        )
    write = {"newline": "\n", "encoding": "utf-8"}
    (EVIDENCE / "data_references.json").write_text(
        json.dumps({"datasets": refs}, indent=2, sort_keys=True) + "\n", **write
    )
    (EVIDENCE / "l2_integrity_report.json").write_text(
        json.dumps(
            {
                "generator": "scripts/verify_l2_integrity.py (stdlib only)",
                "semantics": SEMANTICS,
                "datasets": reports,
                "all_datasets_pass": all_ok,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        **write,
    )
    lines = [
        "# Exact data references (generated by scripts/verify_l2_integrity.py)",
        "",
        "| Symbol | UTC date | Kind | Exact URL | Bytes | SHA-256 | Retrieved (UTC) |",
        "|---|---|---|---|---:|---|---|",
    ]
    for r in refs:
        for kind in ("book", "trades"):
            f = r[kind]
            lines.append(
                f"| {r['symbol']} | {r['utc_date']} | {kind} | {f['url']} | {f['bytes']:,} | "
                f"`{f['sha256']}` | {f['retrieved_at_utc']} |"
            )
    (EVIDENCE / "DATA_REFERENCES.md").write_text("\n".join(lines) + "\n", **write)
    print(f"all datasets pass: {all_ok} -> data/evidence/l2_integrity_report.json")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
