"""No-lookahead verification on the real ORDIUSDT 2026-10-01 replay.

    python scripts/verify_no_lookahead.py [--cut 2026-10-01T08:00:00Z]
    python scripts/verify_no_lookahead.py --scenario best_supported

With --scenario the checks run on that clock against its log in
data/evidence/causal_logs/, "after CUT" means after an event's ready time, and the
independent check is scripts/verify_causal_replay.py.

Three independent lines of evidence, all compared against the delivered audit
log data/evidence/demo_events.jsonl (run ``python scripts/run_demo.py`` first):

1. Independent recomputation (scripts/independent_recompute.py, run in an
   isolated interpreter, standard library only, no bybit_demo import): rebuilds
   trade-flow windows from the raw trade CSV and the order book from the raw
   archive zip using only records with timestamp <= each transition, and
   compares 10 logged features for every transition.
2. Prefix replay: the real day is replayed and stopped at CUT (only the first
   later event's timestamp is inspected, to end the run). Every transition at
   or before CUT must be byte-identical to the full-day log.
3. Future perturbation: the real day is replayed with every event after CUT
   falsified (trade sides flipped and sizes x1000; every book quantity x7, update
   IDs untouched so continuity still holds). Every transition at or before CUT
   must still be byte-identical to the full-day log.

Checks 2 and 3 run the package's own replay, so they test *causality* of that
implementation; check 1 is the implementation-independent part.

Writes data/evidence/no_lookahead_verification.json; exit code 0 if all pass.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Iterable, Iterator
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)

from bybit_demo.audit.logger import MemorySink  # noqa: E402
from bybit_demo.config import load_config  # noqa: E402
from bybit_demo.models.events import (  # noqa: E402
    BookDelta,
    BookEvent,
    BookSnapshot,
    MarketEvent,
    PriceLevel,
    Side,
    TradeEvent,
    iso_to_us,
    us_to_iso,
)
from bybit_demo.pipeline import (  # noqa: E402
    build_clock,
    build_engine,
    load_clock_scenarios,
    open_streams,
)

LOG = ROOT / "data/evidence/demo_events.jsonl"
OUT = ROOT / "data/evidence/no_lookahead_verification.json"


def _falsify(stream: Iterable[MarketEvent], cut_us: int) -> Iterator[MarketEvent]:
    for ev in stream:
        if ev.replay_ts_us <= cut_us:
            yield ev
        elif isinstance(ev, TradeEvent):
            flipped = Side.SELL if ev.side is Side.BUY else Side.BUY
            yield replace(ev, side=flipped, quantity=ev.quantity * 1000)
        elif isinstance(ev, BookDelta):
            yield replace(
                ev,
                bid_updates=tuple(PriceLevel(p.price, p.quantity * 7) for p in ev.bid_updates),
                ask_updates=tuple(PriceLevel(p.price, p.quantity * 7) for p in ev.ask_updates),
            )
        elif isinstance(ev, BookSnapshot):
            yield replace(
                ev,
                bids=tuple(PriceLevel(p.price, p.quantity * 7) for p in ev.bids),
                asks=tuple(PriceLevel(p.price, p.quantity * 7) for p in ev.asks),
            )


def _replay(mode: str, cut: str, scenario: str | None) -> list[str]:
    """Return canonical JSON of every transition with ts <= cut."""
    cfg = load_config(Path("config/demo.yaml"))
    ds = cfg.primary()
    cut_us = iso_to_us(cut)
    streams, _ = open_streams(cfg, ds)
    if scenario is not None:
        scenarios, measured = load_clock_scenarios(cfg)
        s = next(x for x in scenarios if x.name == scenario)
        clock = build_clock(cfg, s, measured)
        assert clock is not None
        book_stream, trade_stream = streams
        streams = [
            clock.stamp_book(cast("Iterable[BookEvent]", book_stream)),
            clock.stamp_trades(cast("Iterable[TradeEvent]", trade_stream)),
        ]
    end = cut
    if mode == "perturbed":
        # Run 10 minutes past the cut so the falsified events are really consumed.
        streams = [_falsify(s, cut_us) for s in streams]
        end = us_to_iso(cut_us + 600_000_000)
    sink = MemorySink()
    build_engine(cfg, ds, [sink]).run(*streams, end_utc=end)
    return [_canon(r.to_dict()) for r in sink.transitions if r.ts_us <= cut_us]


def _canon(d: dict[str, Any]) -> str:
    return json.dumps(json.loads(json.dumps(d)), sort_keys=True, separators=(",", ":"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cut", default="2026-10-01T08:00:00Z", help="UTC cut time (ISO-8601, Z)")
    ap.add_argument("--scenario", help="clock scenario; default: the cts baseline log")
    args = ap.parse_args()
    cut_us = iso_to_us(args.cut)
    log = (
        ROOT / f"data/evidence/causal_logs/{args.scenario}/demo_events.jsonl"
        if args.scenario
        else LOG
    )
    out = ROOT / f"data/evidence/causal_no_lookahead_{args.scenario}.json" if args.scenario else OUT
    if not log.exists():
        print(f"{log} missing: run the replay first")
        return 2
    delivered: list[str] = []
    total = 0
    with log.open(encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            if rec.get("record_type") == "state_transition":
                total += 1
                if rec["ts_us"] <= cut_us:
                    delivered.append(_canon(rec))

    independent = (
        ["scripts/verify_causal_replay.py", args.scenario]
        if args.scenario
        else ["scripts/independent_recompute.py"]
    )
    proc = subprocess.run(
        [sys.executable, "-I", *independent], capture_output=True, text=True, check=False
    )
    with ProcessPoolExecutor(max_workers=2) as pool:
        prefix_f = pool.submit(_replay, "prefix", args.cut, args.scenario)
        perturbed_f = pool.submit(_replay, "perturbed", args.cut, args.scenario)
        prefix, perturbed = prefix_f.result(), perturbed_f.result()

    def compare(got: list[str]) -> dict[str, Any]:
        first = next(
            (i for i, (x, y) in enumerate(zip(got, delivered, strict=False)) if x != y), None
        )
        return {
            "transitions_at_or_before_cut": len(got),
            "identical_to_delivered_log": got == delivered,
            "first_differing_index": first,
        }

    recompute = (
        None
        if args.scenario
        else json.loads((ROOT / "data/evidence/independent_recompute.json").read_text("utf-8"))
    )
    result: dict[str, Any] = {
        "generator": "scripts/verify_no_lookahead.py",
        "cut_utc": args.cut,
        "delivered_log": {
            "transitions_total": total,
            "transitions_at_or_before_cut": len(delivered),
        },
        "independent_recompute": (
            {"exit_code": proc.returncode, "stdout": proc.stdout.strip()}
            if recompute is None
            else {
                "exit_code": proc.returncode,
                "stdout": proc.stdout.strip(),
                "transitions_checked": recompute["transitions_checked"],
                "features_checked_per_transition": recompute["features_checked_per_transition"],
                "mismatching_transitions": recompute["mismatching_transitions"],
            }
        ),
        "prefix_replay": compare(prefix),
        "future_perturbation_replay": {
            **compare(perturbed),
            "perturbation": "after cut: trade side flipped and size x1000; book quantities x7",
        },
    }
    ok = (
        proc.returncode == 0
        and result["prefix_replay"]["identical_to_delivered_log"]
        and result["future_perturbation_replay"]["identical_to_delivered_log"]
        and len(delivered) > 0
    )
    result["pass"] = ok
    if args.scenario:
        result["scenario"] = args.scenario
    out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", "utf-8", newline="\n")
    print(proc.stdout.strip())
    print(
        f"prefix replay to {args.cut}: {len(prefix)} transitions, identical: "
        f"{result['prefix_replay']['identical_to_delivered_log']}; future-perturbed replay: "
        f"{len(perturbed)} transitions, identical: "
        f"{result['future_perturbation_replay']['identical_to_delivered_log']} "
        f"(delivered log has {len(delivered)} transitions <= cut) -> PASS={ok}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
