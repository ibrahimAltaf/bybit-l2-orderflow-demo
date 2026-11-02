"""Raw provider files -> normalized :mod:`bybit_demo.models.events` streams.

Parsers are generators: they read the file front to back and yield events in
file order. They never look ahead to later records when building an event,
except the Tardis book parser, which must read the next row to know where a
group of rows sharing one message timestamp ends (it does not use any value
from that next row).
"""

from __future__ import annotations

import contextlib
import csv
import gzip
import io
import itertools
import json
import zipfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Literal

from bybit_demo.data.validators import (
    BYBIT_TRADE_COLUMNS,
    TARDIS_BOOK_COLUMNS,
    TARDIS_TRADE_COLUMNS,
    ValidationReport,
    parse_decimal,
    require_columns,
    validate_bybit_ob_message,
)
from bybit_demo.errors import CorruptFileError, SchemaError
from bybit_demo.flow.aggressor import parse_reported_side
from bybit_demo.models.events import (
    AggressorSource,
    BookDelta,
    BookEvent,
    BookSnapshot,
    PriceLevel,
    TradeEvent,
)

BookClock = Literal["cts", "ts"]
_US_PER_S = Decimal(1_000_000)


# Decompression failures that mean a file is truncated or corrupt. They are never
# treated as "end of data": the whole file is rejected (CorruptFileError).
_DECOMPRESSION_ERRORS = (zipfile.BadZipFile, zlib.error, EOFError, gzip.BadGzipFile, csv.Error)


@contextlib.contextmanager
def _corruption_guard(source: str) -> Iterator[None]:
    try:
        yield
    except _DECOMPRESSION_ERRORS as exc:
        raise CorruptFileError(
            f"{source}: file is truncated or corrupt ({type(exc).__name__}: {exc}). "
            "Its contents cannot be trusted; delete it and re-download."
        ) from exc


# --------------------------------------------------------------------- Bybit
def _levels(raw: list[list[str]], name: str, source: str, line: int) -> tuple[PriceLevel, ...]:
    out: list[PriceLevel] = []
    seen: set[Decimal] = set()
    for lvl in raw:
        if type(lvl) is not list or len(lvl) != 2:
            raise SchemaError(f"data.{name} level must be [price, qty]", source=source, line=line)
        price = parse_decimal(lvl[0], f"{name}.price", source=source, line=line, positive=True)
        # Two entries for one price in one message have no defined order of
        # application, so the message is rejected rather than "last wins".
        if price in seen:
            raise SchemaError(f"data.{name} lists price {lvl[0]} twice", source=source, line=line)
        seen.add(price)
        out.append(
            PriceLevel(
                price,
                parse_decimal(lvl[1], f"{name}.qty", source=source, line=line, positive=False),
            )
        )
    return tuple(out)


def iter_bybit_orderbook(
    path: Path,
    report: ValidationReport,
    *,
    book_clock: BookClock = "cts",
    symbol: str | None = None,
) -> Iterator[BookEvent]:
    """Yield snapshots/deltas from an official ``*_ob{depth}.data.zip`` archive."""
    source = path.name
    with _corruption_guard(source), zipfile.ZipFile(path) as zf:
        members = zf.namelist()
        if len(members) != 1:
            raise SchemaError(f"expected one member in archive, found {members}", source=source)
        with zf.open(members[0]) as fh:
            for line_no, raw_line in enumerate(fh, start=1):
                try:
                    msg = json.loads(raw_line)
                    validate_bybit_ob_message(msg, source=source, line=line_no)
                    ev = _bybit_ob_event(msg, source, line_no, book_clock)
                except json.JSONDecodeError as exc:
                    report.malformed(
                        SchemaError(f"invalid JSON: {exc}", source=source, line=line_no)
                    )
                    continue
                except SchemaError as exc:
                    report.malformed(exc)
                    continue
                if symbol is not None and ev.symbol != symbol:
                    report.malformed(
                        SchemaError(f"unexpected symbol {ev.symbol}", source=source, line=line_no)
                    )
                    continue
                report.ok()
                yield ev


def _bybit_ob_event(msg: dict[str, object], source: str, line: int, clock: BookClock) -> BookEvent:
    data = msg["data"]
    assert isinstance(data, dict)
    ts_ms = msg["ts"]
    cts_ms = msg.get("cts")
    assert isinstance(ts_ms, int)
    if clock == "cts" and not isinstance(cts_ms, int):
        raise SchemaError("cts required for book_clock=cts", source=source, line=line)
    order_ms = cts_ms if clock == "cts" else ts_ms
    assert isinstance(order_ms, int)
    topic = str(msg["topic"])
    depth = int(topic.split(".")[1]) if topic.count(".") == 2 else None
    common = {
        "exchange": "bybit",
        "symbol": str(data["s"]),
        "exchange_ts_us": order_ms * 1000,
        "update_id": int(data["u"]),
        "cross_seq": int(data["seq"]) if "seq" in data else None,
        "system_ts_us": ts_ms * 1000,
        "engine_ts_us": cts_ms * 1000 if isinstance(cts_ms, int) else None,
        "depth": depth,
        "provider": "bybit_archive",
        "source_line": line,
    }
    bids = _levels(data["b"], "b", source, line)
    asks = _levels(data["a"], "a", source, line)
    if msg["type"] == "snapshot":
        if any(lvl.quantity == 0 for lvl in (*bids, *asks)):
            raise SchemaError("snapshot contains zero-quantity level", source=source, line=line)
        return BookSnapshot(bids=bids, asks=asks, **common)  # type: ignore[arg-type]
    return BookDelta(bid_updates=bids, ask_updates=asks, **common)  # type: ignore[arg-type]


@dataclass(slots=True)
class TradeFileInfo:
    """Facts observed while loading a trade file (reported in evidence)."""

    rows: int = 0
    reordered: bool = False
    out_of_order_rows: int = 0
    duplicate_ids: int = 0
    rpi_trades: int = 0
    columns: list[str] = field(default_factory=list)


def _ts_seconds_to_us(value: str, source: str, line: int) -> int:
    d = parse_decimal(value, "timestamp", source=source, line=line, positive=True)
    return int(d * _US_PER_S)


def load_bybit_trades(
    path: Path, report: ValidationReport, *, symbol: str | None = None
) -> tuple[list[TradeEvent], TradeFileInfo]:
    """Load an official ``{SYMBOL}{date}.csv.gz`` trade file.

    The file is loaded whole and, *only if* it is not already chronological,
    stably sorted by timestamp. Sorting changes ordering only -- it creates no
    information -- and the fact is recorded in :class:`TradeFileInfo`.
    """
    source = path.name
    info = TradeFileInfo()
    trades: list[TradeEvent] = []
    seen: set[str] = set()
    with _corruption_guard(source), gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        header = list(reader.fieldnames or [])
        info.columns = header
        require_columns(header, BYBIT_TRADE_COLUMNS, source=source)
        for line_no, row in enumerate(reader, start=2):
            try:
                t = _bybit_trade(row, source, line_no)
            except SchemaError as exc:
                report.malformed(exc)
                continue
            if symbol is not None and t.symbol != symbol:
                report.malformed(
                    SchemaError(f"unexpected symbol {t.symbol}", source=source, line=line_no)
                )
                continue
            if t.trade_id is not None:
                if t.trade_id in seen:
                    info.duplicate_ids += 1
                    report.malformed(
                        SchemaError(f"duplicate trade id {t.trade_id}", source=source, line=line_no)
                    )
                    continue
                seen.add(t.trade_id)
            if t.raw.get("RPI") == "1":
                info.rpi_trades += 1
            report.ok()
            trades.append(t)
    info.rows = len(trades)
    info.out_of_order_rows = sum(
        1 for a, b in itertools.pairwise(trades) if b.exchange_ts_us < a.exchange_ts_us
    )
    if info.out_of_order_rows:
        trades.sort(key=lambda t: t.exchange_ts_us)  # stable: keeps file order within a timestamp
        info.reordered = True
    return trades, info


def _bybit_trade(row: dict[str, str | None], source: str, line: int) -> TradeEvent:
    def col(name: str) -> str:
        v = row.get(name)
        if v is None or v == "":
            raise SchemaError(f"missing value for {name!r}", source=source, line=line)
        return v

    try:
        side = parse_reported_side(col("side"))
    except SchemaError as exc:
        raise SchemaError(str(exc), source=source, line=line) from exc
    raw = {k: v for k in ("tickDirection", "RPI") if (v := row.get(k)) not in (None, "")}
    return TradeEvent(
        exchange="bybit",
        symbol=col("symbol"),
        exchange_ts_us=_ts_seconds_to_us(col("timestamp"), source, line),
        price=parse_decimal(col("price"), "price", source=source, line=line, positive=True),
        quantity=parse_decimal(col("size"), "size", source=source, line=line, positive=True),
        side=side,
        aggressor_source=AggressorSource.REPORTED,
        trade_id=row.get("trdMatchID") or None,
        provider="bybit_archive",
        raw=raw,
    )


# -------------------------------------------------------------------- Tardis
def _open_csv(path: Path) -> io.TextIOWrapper:
    return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8", newline="")


def iter_tardis_book(path: Path, report: ValidationReport) -> Iterator[BookEvent]:
    """Group Tardis ``incremental_book_L2`` rows into one event per (timestamp, is_snapshot) run."""
    source = path.name
    with _corruption_guard(source), _open_csv(path) as fh:
        reader = csv.reader(fh)
        header = next(reader)
        require_columns(header, TARDIS_BOOK_COLUMNS, source=source)
        ix = {c: header.index(c) for c in TARDIS_BOOK_COLUMNS}
        key: tuple[int, int, bool] | None = None
        symbol = ""
        bids: list[PriceLevel] = []
        asks: list[PriceLevel] = []
        first_line = 2

        def flush() -> BookEvent:
            assert key is not None
            ts, local, snap = key
            common = {
                "exchange": "bybit",
                "symbol": symbol,
                "exchange_ts_us": ts,
                "local_ts_us": local,
                "provider": "tardis_free",
                "source_line": first_line,
            }
            if snap:
                return BookSnapshot(bids=tuple(bids), asks=tuple(asks), **common)  # type: ignore[arg-type]
            return BookDelta(bid_updates=tuple(bids), ask_updates=tuple(asks), **common)  # type: ignore[arg-type]

        for line_no, row in enumerate(reader, start=2):
            try:
                if len(row) != len(header):
                    raise SchemaError(
                        f"expected {len(header)} columns, got {len(row)}",
                        source=source,
                        line=line_no,
                    )
                ts = int(row[ix["timestamp"]])
                local = int(row[ix["local_timestamp"]])
                snap_s = row[ix["is_snapshot"]]
                if snap_s not in ("true", "false"):
                    raise SchemaError(f"is_snapshot={snap_s!r}", source=source, line=line_no)
                side = row[ix["side"]]
                if side not in ("bid", "ask"):
                    raise SchemaError(f"side={side!r}", source=source, line=line_no)
                lvl = PriceLevel(
                    parse_decimal(
                        row[ix["price"]], "price", source=source, line=line_no, positive=True
                    ),
                    parse_decimal(
                        row[ix["amount"]], "amount", source=source, line=line_no, positive=False
                    ),
                )
            except (SchemaError, ValueError) as exc:
                err = (
                    exc
                    if isinstance(exc, SchemaError)
                    else SchemaError(str(exc), source=source, line=line_no)
                )
                report.malformed(err)
                continue
            report.ok()
            this_key = (ts, local, snap_s == "true")
            if key is not None and this_key != key:
                yield flush()
                bids, asks = [], []
                first_line = line_no
            key = this_key
            symbol = row[ix["symbol"]]
            (bids if side == "bid" else asks).append(lvl)
        if key is not None:
            yield flush()


def load_tardis_trades(
    path: Path, report: ValidationReport
) -> tuple[list[TradeEvent], TradeFileInfo]:
    source = path.name
    info = TradeFileInfo()
    trades: list[TradeEvent] = []
    seen: set[str] = set()
    with _corruption_guard(source), _open_csv(path) as fh:
        reader = csv.DictReader(fh)
        header = list(reader.fieldnames or [])
        info.columns = header
        require_columns(header, TARDIS_TRADE_COLUMNS, source=source)
        for line_no, row in enumerate(reader, start=2):
            try:
                tid = row["id"] or None
                t = TradeEvent(
                    exchange="bybit",
                    symbol=row["symbol"],
                    exchange_ts_us=int(row["timestamp"]),
                    local_ts_us=int(row["local_timestamp"]),
                    price=parse_decimal(
                        row["price"], "price", source=source, line=line_no, positive=True
                    ),
                    quantity=parse_decimal(
                        row["amount"], "amount", source=source, line=line_no, positive=True
                    ),
                    side=parse_reported_side(row["side"]),
                    aggressor_source=AggressorSource.REPORTED,
                    trade_id=tid,
                    provider="tardis_free",
                )
            except (SchemaError, ValueError, KeyError) as exc:
                report.malformed(SchemaError(str(exc), source=source, line=line_no))
                continue
            if tid is not None:
                if tid in seen:
                    info.duplicate_ids += 1
                    continue
                seen.add(tid)
            report.ok()
            trades.append(t)
    info.rows = len(trades)
    info.out_of_order_rows = sum(
        1 for a, b in itertools.pairwise(trades) if b.exchange_ts_us < a.exchange_ts_us
    )
    if info.out_of_order_rows:
        trades.sort(key=lambda t: t.exchange_ts_us)
        info.reordered = True
    return trades, info
