"""Independent audit: recompute logged features from the raw files.

Deliberately uses only the Python standard library and does NOT import the
``bybit_demo`` package, so it checks the pipeline instead of re-running it.

1. Provenance: the SHA-256 of both raw files must equal the hashes recorded in
   the header of data/evidence/demo_events.jsonl.
2. For every state transition in demo_events.jsonl, using only raw records
   with timestamp <= the transition time:
   * trade flow (raw trade CSV): 5 s and 30 s rolling CVD (signed USDT
     notional), 30 s notional imbalance, 5 s trade count, session CVD;
   * order book (raw archive zip, its own minimal snapshot/delta replay on the
     ``cts`` clock): best bid, best ask, last applied update ID, and resting
     notional within the gate depth band on each side.
   Every logged value must match. A feature that used a later record would
   disagree with this recomputation.

Writes data/evidence/independent_recompute.json.

    python scripts/independent_recompute.py
"""

from __future__ import annotations

import bisect
import csv
import gzip
import hashlib
import json
import sys
import zipfile
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EVENTS = ROOT / "data/evidence/demo_events.jsonl"
OUT = ROOT / "data/evidence/independent_recompute.json"
TOL = 1e-6  # logged floats are rounded to 6 decimals
BPS = Decimal(10_000)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def read_events() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    header: dict[str, Any] = {}
    transitions: list[dict[str, Any]] = []
    with EVENTS.open(encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            if rec.get("record_type") == "run_header":
                header = rec
            elif rec.get("record_type") == "state_transition":
                transitions.append(rec)
    if not header:
        raise SystemExit(f"{EVENTS}: no header record")
    return header, transitions


class TradeFlow:
    """Prefix sums over the raw trade CSV (file must be chronological)."""

    def __init__(self, path: Path) -> None:
        self.ts: list[int] = []
        signed: list[Decimal] = []
        with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                # Decimal("1790812802.1187") * 10**6 is exact; int() truncates nothing.
                self.ts.append(int(Decimal(row["timestamp"]) * 1_000_000))
                n = Decimal(row["price"]) * Decimal(row["size"])
                signed.append(n if row["side"] == "Buy" else -n)
        if self.ts != sorted(self.ts):
            raise SystemExit("raw trade file not chronological; this script assumes it is")
        self.cum_s = [Decimal(0)]
        self.cum_a = [Decimal(0)]
        for v in signed:
            self.cum_s.append(self.cum_s[-1] + v)
            self.cum_a.append(self.cum_a[-1] + abs(v))

    def window(self, t: int, w: int) -> tuple[Decimal, Decimal, int]:
        """(signed, gross, count) over trades with timestamp in (t - w, t]."""
        hi = bisect.bisect_right(self.ts, t)
        lo = bisect.bisect_right(self.ts, t - w)
        return self.cum_s[hi] - self.cum_s[lo], self.cum_a[hi] - self.cum_a[lo], hi - lo

    def features(self, t: int) -> dict[str, float]:
        s5, _, n5 = self.window(t, 5_000_000)
        s30, a30, _ = self.window(t, 30_000_000)
        return {
            "cvd_short_usdt": float(s5),
            "cvd_medium_usdt": float(s30),
            "imb_medium": float(s30 / a30) if a30 else 0.0,
            "trades_short": n5,
            "cvd_session_usdt": float(self.cum_s[bisect.bisect_right(self.ts, t)]),
        }


def book_messages(path: Path) -> Iterator[tuple[int, dict[str, object]]]:
    """(cts in us, message) for every line of the archive, in file order."""
    with zipfile.ZipFile(path) as zf, zf.open(zf.namelist()[0]) as fh:
        for line in fh:
            msg = json.loads(line)
            yield int(msg["cts"]) * 1000, msg


class MiniBook:
    """Minimal independent book: snapshot replaces, delta sets, qty 0 deletes."""

    def __init__(self) -> None:
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.u: int | None = None

    def apply(self, msg: dict[str, object]) -> None:
        data = msg["data"]
        assert isinstance(data, dict)
        if msg["type"] == "snapshot":
            self.bids.clear()
            self.asks.clear()
        for side, book in (("b", self.bids), ("a", self.asks)):
            for p, q in data[side]:
                price, qty = Decimal(p), Decimal(q)
                if qty == 0:
                    book.pop(price, None)
                else:
                    book[price] = qty
        self.u = int(data["u"])

    def features(self, band_bps: Decimal) -> dict[str, object]:
        bid, ask = max(self.bids), min(self.asks)
        mid = (bid + ask) / 2
        off = mid * band_bps / BPS
        return {
            "best_bid": bid,
            "best_ask": ask,
            "update_id": self.u,
            "depth_bid_usdt": float(sum(p * q for p, q in self.bids.items() if p >= mid - off)),
            "depth_ask_usdt": float(sum(p * q for p, q in self.asks.items() if p <= mid + off)),
        }


def differs(ours: object, logged: object) -> bool:
    if isinstance(ours, Decimal):
        return ours != Decimal(str(logged))
    if isinstance(ours, int) or ours is None:
        return ours != logged
    assert isinstance(ours, float) and isinstance(logged, int | float)
    return abs(ours - float(logged)) > TOL * max(1.0, abs(ours))


def main() -> int:
    header, transitions = read_events()
    book_path = ROOT / str(header["book_file"])
    trades_path = ROOT / str(header["trades_file"])
    provenance = {
        "book_file": book_path.relative_to(ROOT).as_posix(),
        "trades_file": trades_path.relative_to(ROOT).as_posix(),
        "book_sha256_matches_header": sha256(book_path) == header["book_file_sha256"],
        "trades_sha256_matches_header": sha256(trades_path) == header["trades_file_sha256"],
    }
    if header.get("book_clock") != "cts":
        raise SystemExit("this script recomputes the book on the cts clock only")
    bands = {
        r["thresholds"]["gates.depth_band_bps"]
        for r in transitions
        if "gates.depth_band_bps" in r["thresholds"]
    }
    if len(bands) != 1:
        raise SystemExit(f"expected one gate depth band in thresholds, found {bands}")
    band = Decimal(str(bands.pop()))

    flow = TradeFlow(trades_path)
    book = MiniBook()
    msgs = book_messages(book_path)
    pending = next(msgs, None)
    mismatches: list[dict[str, object]] = []
    for rec in transitions:  # chronological in the evidence file
        t = rec["ts_us"]
        assert isinstance(t, int)
        while pending is not None and pending[0] <= t:
            book.apply(pending[1])
            pending = next(msgs, None)
        logged = rec["features"]
        assert isinstance(logged, dict)
        ours: dict[str, object] = {**flow.features(t), **book.features(band)}
        ours_u = ours.pop("update_id")
        diff = {k: (str(v), logged[k]) for k, v in ours.items() if differs(v, logged[k])}
        if ours_u != rec["update_id"]:
            diff["update_id"] = (str(ours_u), rec["update_id"])
        if diff:
            mismatches.append(
                {
                    "timestamp": rec["timestamp"],
                    "direction": rec["direction"],
                    "transition": f"{rec['previous_state']}->{rec['new_state']}",
                    "recomputed_vs_logged": diff,
                }
            )
    result: dict[str, Any] = {
        "method": __doc__.strip().splitlines()[0],
        "provenance": provenance,
        "transitions_checked": len(transitions),
        "features_checked_per_transition": [
            "cvd_short_usdt",
            "cvd_medium_usdt",
            "imb_medium",
            "trades_short",
            "cvd_session_usdt",
            "best_bid",
            "best_ask",
            "update_id",
            "depth_bid_usdt",
            "depth_ask_usdt",
        ],
        "gate_depth_band_bps": str(band),
        "tolerance_relative": TOL,
        "mismatching_transitions": len(mismatches),
        "first_mismatches": mismatches[:20],
    }
    OUT.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    ok = not mismatches and all(provenance[k] for k in provenance if k.endswith("_header"))
    print(
        f"checked {len(transitions)} transitions x {len(result['features_checked_per_transition'])}"
        f" features; mismatches: {len(mismatches)}; raw-file hashes match evidence header: "
        f"{provenance['book_sha256_matches_header'] and provenance['trades_sha256_matches_header']}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
