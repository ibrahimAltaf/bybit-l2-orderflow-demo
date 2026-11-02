"""SYNTHETIC market scenarios -- NOT real market data, NEVER client evidence.

Deterministic, hand-constructed event sequences used to unit/integration-test
the state machine. Symbol is ``SYNTHUSDT`` and timestamps start at
2000-01-01T00:00:00Z so they cannot be mistaken for real Bybit data.

Each scenario is designed (with the thresholds in config/demo.yaml) to drive
a specific code path; that is the point of a fixture and says nothing about
real markets.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import zipfile
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from bybit_demo.models.events import (
    AggressorSource,
    BookDelta,
    BookSnapshot,
    PriceLevel,
    Side,
    TradeEvent,
)

SYMBOL = "SYNTHUSDT"
T0_US = 946_684_800 * 1_000_000  # 2000-01-01T00:00:00Z -- deliberately implausible
TICK = Decimal("0.01")
MARKER = "SYNTHETIC FIXTURE - NOT REAL MARKET DATA"


@dataclass
class Scenario:
    name: str
    description: str
    book: list[BookSnapshot | BookDelta] = field(default_factory=list)
    trades: list[TradeEvent] = field(default_factory=list)


class _Builder:
    def __init__(self, name: str, description: str) -> None:
        self.sc = Scenario(name, description)
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.u = 1000
        self.seq = 50_000
        self.trade_n = 0
        self.skip_next_u = False

    @staticmethod
    def us(t_s: float) -> int:
        return T0_US + round(t_s * 1000) * 1000  # millisecond grid, like Bybit

    def snapshot(self, t_s: float) -> None:
        self.u += 1
        self.seq += 1
        ts = self.us(t_s)
        self.sc.book.append(
            BookSnapshot(
                exchange="bybit",
                symbol=SYMBOL,
                exchange_ts_us=ts,
                bids=tuple(PriceLevel(p, q) for p, q in sorted(self.bids.items(), reverse=True)),
                asks=tuple(PriceLevel(p, q) for p, q in sorted(self.asks.items())),
                update_id=self.u,
                cross_seq=self.seq,
                system_ts_us=ts,
                engine_ts_us=ts,
                depth=200,
                provider="synthetic",
            )
        )

    def delta(
        self,
        t_s: float,
        bids: dict[Decimal, Decimal] | None = None,
        asks: dict[Decimal, Decimal] | None = None,
    ) -> None:
        bids, asks = bids or {}, asks or {}
        for book, upd in ((self.bids, bids), (self.asks, asks)):
            for p, q in upd.items():
                if q == 0:
                    book.pop(p, None)
                else:
                    book[p] = q
        self.u += 2 if self.skip_next_u else 1
        self.skip_next_u = False
        self.seq += 1
        ts = self.us(t_s)
        self.sc.book.append(
            BookDelta(
                exchange="bybit",
                symbol=SYMBOL,
                exchange_ts_us=ts,
                bid_updates=tuple(PriceLevel(p, q) for p, q in bids.items()),
                ask_updates=tuple(PriceLevel(p, q) for p, q in asks.items()),
                update_id=self.u,
                cross_seq=self.seq,
                system_ts_us=ts,
                engine_ts_us=ts,
                depth=200,
                provider="synthetic",
            )
        )

    def trade(self, t_s: float, side: Side, price: Decimal, qty: Decimal) -> None:
        self.trade_n += 1
        self.sc.trades.append(
            TradeEvent(
                exchange="bybit",
                symbol=SYMBOL,
                exchange_ts_us=self.us(t_s),
                price=price,
                quantity=qty,
                side=side,
                aggressor_source=AggressorSource.REPORTED,
                trade_id=f"synthetic-{self.trade_n:06d}",
                provider="synthetic",
            )
        )

    def check_chronological(self) -> None:
        for stream in (self.sc.book, self.sc.trades):
            ts = [e.exchange_ts_us for e in stream]
            if ts != sorted(ts):
                raise AssertionError(f"{self.sc.name}: fixture stream not chronological")

    def shift(self, t_s: float, ticks: int) -> None:
        """Move the whole book by ``ticks`` (one delta with deletions + insertions)."""
        off = TICK * ticks
        nb = {p + off: q for p, q in self.bids.items()}
        na = {p + off: q for p, q in self.asks.items()}
        bupd = {p: Decimal(0) for p in self.bids if p not in nb} | nb
        aupd = {p: Decimal(0) for p in self.asks if p not in na} | na
        self.delta(t_s, bupd, aupd)


def _breakout(
    name: str,
    description: str,
    *,
    direction: int,
    pre_move_ticks: int = 0,
    gap_at_s: float | None = None,
    reversal_at_s: float | None = None,
) -> Scenario:
    """Balanced warm-up, then persistent one-sided aggression into a thinning book.

    ``direction`` +1 builds a buy-side breakout; -1 the exact mirror.
    """
    b = _Builder(name, description)
    mid = Decimal(100)
    best_bid, best_ask = mid - TICK, mid + TICK
    for i in range(30):
        b.bids[best_bid - TICK * i] = Decimal(100 if i == 0 else 20)
        b.asks[best_ask + TICK * i] = Decimal(100 if i == 0 else 20)
    b.snapshot(0.0)

    deep_bid = best_bid - TICK * 29
    offset = 0  # ticks the whole book has been shifted (pre-move scenarios)
    inserted: Decimal | None = None  # transient bid used to create small mid oscillations
    move_end = 320 + 2 * abs(pre_move_ticks)

    # --- warm-up 0-400 s: balanced flow, small mid oscillation, 1 s heartbeat --
    # Single chronological loop on a 0.25 s grid so prices always match the
    # current book and update IDs are assigned in time order.
    k = 0
    for step in range(1, 1600):
        t = step * 0.25
        whole = step % 4 == 0
        sec = step // 4
        if whole and pre_move_ticks and 320 <= sec < move_end and sec % 2 == 0:
            if inserted is not None:
                b.delta(t, bids={inserted: Decimal(0)})
                inserted = None
            step_ticks = 1 if pre_move_ticks > 0 else -1
            b.shift(t, step_ticks)
            offset += step_ticks
        if step % 2 == 0:
            side = Side.BUY if k % 2 == 0 else Side.SELL
            price = (best_ask if side is Side.BUY else best_bid) + TICK * offset
            b.trade(t, side, price, Decimal(1))
            k += 1
        in_move = bool(pre_move_ticks) and 315 <= sec < move_end + 5
        if whole and sec % 10 == 5 and inserted is None and not in_move:
            inserted = best_bid + TICK * (offset + 1)
            b.delta(t, bids={inserted: Decimal(20)})
        if step % 4 == 2 and sec % 10 == 9 and inserted is not None:
            b.delta(t, bids={inserted: Decimal(0)})
            inserted = None
        if step % 4 == 3:
            b.delta(t, bids={deep_bid + TICK * offset: Decimal(20 + sec % 2)})

    # --- breakout phase 400-425 s ---------------------------------------------
    main = Side.BUY if direction > 0 else Side.SELL
    counter = Side.SELL if direction > 0 else Side.BUY
    res_book, sup_book = (b.asks, b.bids) if direction > 0 else (b.bids, b.asks)
    res_l1 = (best_ask if direction > 0 else best_bid) + TICK * offset
    sup_l1 = (best_bid if direction > 0 else best_ask) + TICK * offset
    sgn = TICK if direction > 0 else -TICK

    def res_delta(t_s: float, upd: dict[Decimal, Decimal]) -> None:
        if direction > 0:
            b.delta(t_s, asks=upd)
        else:
            b.delta(t_s, bids=upd)

    def sup_delta(t_s: float, upd: dict[Decimal, Decimal]) -> None:
        if direction > 0:
            b.delta(t_s, bids=upd)
        else:
            b.delta(t_s, asks=upd)

    withdrawn = 0
    for n in range(100):  # 25 s on a 0.25 s grid
        t = 400.0 + n * 0.25
        if gap_at_s is not None and abs(t - gap_at_s) < 1e-9:
            b.skip_next_u = True
        reversed_ = reversal_at_s is not None and t >= reversal_at_s
        aggressor = counter if reversed_ else main
        if n % 5 == 4:
            aggressor = main if reversed_ else counter
        restore = False
        if aggressor is main:
            qty = Decimal(2)
            if res_book.get(res_l1, Decimal(0)) > qty:
                b.trade(t, main, res_l1, qty)
                res_delta(t, {res_l1: res_book[res_l1] - qty})
        else:
            qty = Decimal(10) if reversed_ else Decimal(1)
            b.trade(t, counter, sup_l1, qty)
            sup_delta(t, {sup_l1: sup_book[sup_l1] - qty})
            restore = True
        if n % 4 == 0 and n > 0 and withdrawn < 10:
            withdrawn += 1
            res_delta(t + 0.05, {res_l1 + sgn * withdrawn: Decimal(6)})  # resistance withdrawal
        if restore:
            sup_delta(t + 0.1, {sup_l1: Decimal(100)})  # support replenished
    b.check_chronological()
    return b.sc


def long_breakout() -> Scenario:
    return _breakout(
        "long_breakout",
        "Buy aggression + ask withdrawal + bid replenishment after a quiet warm-up. "
        "Expected: LONG IDLE -> WATCH -> CANDIDATE -> CONFIRMED.",
        direction=+1,
    )


def short_breakdown() -> Scenario:
    return _breakout(
        "short_breakdown",
        "Exact mirror of long_breakout. Expected: SHORT IDLE -> WATCH -> CANDIDATE -> CONFIRMED.",
        direction=-1,
    )


def late_entry() -> Scenario:
    return _breakout(
        "late_entry",
        "Book already moved +12 ticks within the extension window before the buy flow. "
        "Expected: LONG rejected with ALREADY_EXTENDED; never CONFIRMED.",
        direction=+1,
        pre_move_ticks=12,
    )


def sequence_gap() -> Scenario:
    return _breakout(
        "sequence_gap",
        "long_breakout with one update ID skipped at t=402.5 s. Expected: SEQUENCE_GAP "
        "logged, book invalidated, active setup REJECTED, no CONFIRMED.",
        direction=+1,
        gap_at_s=402.5,
    )


def flow_reversal() -> Scenario:
    return _breakout(
        "flow_reversal",
        "Buy aggression that flips to heavy selling at t=404 s. Expected: LONG setup "
        "REJECTED with FLOW_REVERSAL; no LONG CONFIRMED.",
        direction=+1,
        reversal_at_s=404.0,
    )


ALL = (long_breakout, short_breakdown, late_entry, sequence_gap, flow_reversal)


# ------------------------------------------------------------ file writers
def write_bybit_format(sc: Scenario, out_dir: Path) -> tuple[Path, Path]:
    """Write the scenario in Bybit archive formats (exercises the real parsers)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    book_path = out_dir / f"SYNTHETIC_{sc.name}_ob200.data.zip"
    trades_path = out_dir / f"SYNTHETIC_{sc.name}_trades.csv.gz"
    lines = []
    for ev in sc.book:
        is_snap = isinstance(ev, BookSnapshot)
        bids = ev.bids if isinstance(ev, BookSnapshot) else ev.bid_updates
        asks = ev.asks if isinstance(ev, BookSnapshot) else ev.ask_updates
        ms = ev.exchange_ts_us // 1000
        lines.append(
            json.dumps(
                {
                    "topic": f"orderbook.200.{SYMBOL}",
                    "type": "snapshot" if is_snap else "delta",
                    "ts": ms,
                    "cts": ms,
                    "data": {
                        "s": SYMBOL,
                        "b": [[str(lv.price), str(lv.quantity)] for lv in bids],
                        "a": [[str(lv.price), str(lv.quantity)] for lv in asks],
                        "u": ev.update_id,
                        "seq": ev.cross_seq,
                    },
                },
                separators=(",", ":"),
            )
        )
    with zipfile.ZipFile(book_path, "w", zipfile.ZIP_DEFLATED) as zf:
        info = zipfile.ZipInfo(f"SYNTHETIC_{sc.name}_ob200.data", date_time=(2000, 1, 1, 0, 0, 0))
        zf.writestr(info, "\n".join(lines) + "\n")
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(
        ["timestamp", "symbol", "side", "size", "price", "tickDirection", "trdMatchID", "RPI"]
    )
    for tr in sc.trades:
        secs = Decimal(tr.exchange_ts_us) / Decimal(1_000_000)
        w.writerow(
            [
                f"{secs:.3f}",
                SYMBOL,
                tr.side.value,
                str(tr.quantity),
                str(tr.price),
                "",
                tr.trade_id,
                "0",
            ]
        )
    with gzip.GzipFile(trades_path, "wb", mtime=0) as gz:
        gz.write(buf.getvalue().encode())
    return book_path, trades_path


if __name__ == "__main__":
    here = Path(__file__).parent
    for factory in ALL:
        write_bybit_format(factory(), here / "files")
    (here / "files" / "README.txt").write_text(
        MARKER + "\nGenerated by scenarios.py\n", encoding="utf-8"
    )
    print(f"wrote {len(ALL)} synthetic scenarios to {here / 'files'}")
