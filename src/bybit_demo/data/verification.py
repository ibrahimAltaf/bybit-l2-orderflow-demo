"""Dataset verification: the only code path allowed to produce "VERIFIED" facts.

For each configured dataset it parses *every* record of the downloaded files
and measures, rather than assumes: schema fields, snapshot/delta counts,
update-ID increments, cross-sequence monotonicity, timestamp regressions,
publish latency (ts - cts), book depth vs. the advertised depth, whether all
timestamps fall inside the dataset's UTC day, crossed books, level-by-level
agreement of the reconstructed book with every later snapshot record, and how
each reported trade side relates to the book just before the trade.
"""

from __future__ import annotations

import json
import zipfile
from bisect import bisect_left
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import date as date_t
from decimal import Decimal
from pathlib import Path
from typing import Any

from bybit_demo.book.continuity import (
    BookSynchronizer,
    BybitContinuityValidator,
    TimestampOnlyContinuityValidator,
)
from bybit_demo.book.reconstruction import OrderBook
from bybit_demo.config import DatasetConfig, DemoConfig, ProviderId
from bybit_demo.data.downloader import DownloadRecord
from bybit_demo.data.normalization import (
    BookClock,
    TradeFileInfo,
    iter_bybit_orderbook,
    iter_tardis_book,
    load_bybit_trades,
    load_tardis_trades,
)
from bybit_demo.data.provider_registry import FileKind, dataset_files
from bybit_demo.data.validators import ValidationReport
from bybit_demo.models.events import (
    BookDelta,
    BookEvent,
    BookSnapshot,
    MarketEvent,
    Side,
    TradeEvent,
    us_to_iso,
)
from bybit_demo.replay.engine import merge_streams

# The archive's first snapshot is stamped just before 00:00 UTC and its last
# record just after the next 00:00 (observed: -83 ms / +211 ms). Book records
# further than this outside the day indicate a wrong file.
BOOK_DAY_TOLERANCE = timedelta(seconds=60)


def _percentiles(values: list[int], qs: Iterable[float]) -> dict[str, int]:
    if not values:
        return {}
    s = sorted(values)
    return {f"p{q * 100:g}": s[min(len(s) - 1, int(q * len(s)))] for q in qs} | {"max": s[-1]}


def raw_bybit_fields(path: Path, max_lines: int = 2000) -> list[str]:
    """Union of JSON keys present in the first ``max_lines`` raw archive lines (schema evidence)."""
    keys: set[str] = set()
    with zipfile.ZipFile(path) as zf, zf.open(zf.namelist()[0]) as fh:
        for i, line in enumerate(fh):
            if i >= max_lines:
                break
            msg = json.loads(line)
            keys |= set(msg)
            keys |= {f"data.{k}" for k in msg.get("data", {})}
    return sorted(keys)


@dataclass(slots=True)
class _BookScan:
    snapshots: int = 0
    deltas: int = 0
    first_ts_us: int | None = None
    last_ts_us: int | None = None
    max_bid_levels: int = 0
    max_ask_levels: int = 0


class AggressorCheck:
    """Where each reported-side trade printed relative to the book just before it.

    If ``side`` is the taker side, a Buy lifts the ask (price >= best ask) and a
    Sell hits the bid (price <= best bid); if it were the maker side, the
    opposite would hold. The book is the reconstructed state before the trade
    (ties: trades first). On a 100 ms-batched feed that state can be slightly
    stale, so a minority of "inside spread" / "opposite" prints is expected.
    """

    __slots__ = ("counts",)

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()

    def observe(self, trade: TradeEvent, sync: BookSynchronizer) -> None:
        top = sync.book.top() if sync.usable else None
        if top is None:
            self.counts["no_usable_book"] += 1
            return
        p = trade.price
        if p >= top.best_ask:
            where = "at_or_above_ask"
        elif p <= top.best_bid:
            where = "at_or_below_bid"
        else:
            where = "inside_spread"
        self.counts[f"{trade.side.value.lower()}_{where}"] += 1

    def to_dict(self) -> dict[str, Any]:
        c = self.counts
        taker = c["buy_at_or_above_ask"] + c["sell_at_or_below_bid"]
        maker = c["buy_at_or_below_bid"] + c["sell_at_or_above_ask"]
        classified = taker + maker + c["buy_inside_spread"] + c["sell_inside_spread"]
        return {
            "method": (
                "Each trade's price vs. the reconstructed best bid/ask immediately before it "
                "(trades ordered before book updates with the same timestamp)."
            ),
            "counts": dict(sorted(c.items())),
            "consistent_with_taker_side": taker,
            "consistent_with_maker_side": maker,
            "taker_consistent_share": round(taker / classified, 6) if classified else None,
            "maker_consistent_share": round(maker / classified, 6) if classified else None,
        }


def _day_bounds_us(day: date_t) -> tuple[int, int]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    lo = (start - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1)
    return lo, lo + 86_400_000_000


def verify_book(
    events: Iterable[MarketEvent],
    sync: BookSynchronizer,
    *,
    has_update_ids: bool,
    aggressor: AggressorCheck | None = None,
) -> dict[str, Any]:
    """Scan every book event; trades in ``events`` only feed ``aggressor``."""
    scan = _BookScan()
    u_diff: Counter[int] = Counter()
    latency_ms: list[int] = []
    seq_nonincreasing = 0
    system_ts_regressions = 0
    prev_system_ts: int | None = None
    gaps_clock_ms: list[int] = []
    gaps_publish_ms: list[int] = []
    prev_delta: BookEvent | None = None
    prev_u: int | None = None
    prev_seq: int | None = None
    snapshot_log: list[dict[str, Any]] = []
    first_delta_after_snapshot: dict[str, Any] | None = None
    last_snapshot_u: int | None = None
    for ev in events:
        if isinstance(ev, TradeEvent):
            if aggressor is not None:
                aggressor.observe(ev, sync)
            continue
        t = ev.exchange_ts_us
        scan.first_ts_us = t if scan.first_ts_us is None else scan.first_ts_us
        scan.last_ts_us = t
        if ev.system_ts_us is not None and ev.engine_ts_us is not None:
            latency_ms.append((ev.system_ts_us - ev.engine_ts_us) // 1000)
        if ev.system_ts_us is not None:
            if prev_system_ts is not None and ev.system_ts_us < prev_system_ts:
                system_ts_regressions += 1
            prev_system_ts = ev.system_ts_us
        if isinstance(ev, BookSnapshot):
            scan.snapshots += 1
            last_snapshot_u = ev.update_id
        else:
            scan.deltas += 1
            if prev_delta is not None:
                gaps_clock_ms.append((t - prev_delta.exchange_ts_us) // 1000)
                if ev.system_ts_us is not None and prev_delta.system_ts_us is not None:
                    gaps_publish_ms.append((ev.system_ts_us - prev_delta.system_ts_us) // 1000)
            prev_delta = ev
            if has_update_ids and prev_u is not None and ev.update_id is not None:
                u_diff[ev.update_id - prev_u] += 1
            if first_delta_after_snapshot is None and last_snapshot_u is not None:
                first_delta_after_snapshot = {
                    "snapshot_u": last_snapshot_u,
                    "first_delta_u": ev.update_id,
                }
        if (
            has_update_ids
            and ev.cross_seq is not None
            and prev_seq is not None
            and ev.cross_seq <= prev_seq
            and isinstance(ev, BookDelta)
        ):
            seq_nonincreasing += 1
        last_applied_u = sync.book.last_update_id if sync.book.is_initialized else None
        prev_u, prev_seq = ev.update_id, ev.cross_seq
        upd = sync.process(ev)
        if upd.is_snapshot:
            snapshot_log.append(
                {
                    "timestamp": us_to_iso(t),
                    "update_id": ev.update_id,
                    "u_minus_last_applied_u": (
                        None
                        if last_applied_u is None or ev.update_id is None
                        else ev.update_id - last_applied_u
                    ),
                    "bid_levels": len(ev.bids) if isinstance(ev, BookSnapshot) else None,
                    "ask_levels": len(ev.asks) if isinstance(ev, BookSnapshot) else None,
                    "reconstructed_book_matches_snapshot": (
                        None if upd.snapshot_comparison is None else upd.snapshot_comparison.matches
                    ),
                    "mismatched_levels": (
                        None
                        if upd.snapshot_comparison is None
                        else upd.snapshot_comparison.bid_mismatches
                        + upd.snapshot_comparison.ask_mismatches
                    ),
                }
            )
        nb, na = sync.book.level_counts()
        scan.max_bid_levels = max(scan.max_bid_levels, nb)
        scan.max_ask_levels = max(scan.max_ask_levels, na)

    top = sync.book.top()
    st = sync.stats
    out: dict[str, Any] = {
        "first_ts_us": scan.first_ts_us,
        "last_ts_us": scan.last_ts_us,
        "messages": scan.snapshots + scan.deltas,
        "snapshots": scan.snapshots,
        "deltas": scan.deltas,
        "first_ts": us_to_iso(scan.first_ts_us) if scan.first_ts_us else None,
        "last_ts": us_to_iso(scan.last_ts_us) if scan.last_ts_us else None,
        "deltas_applied": st.deltas_applied,
        "deltas_skipped": st.deltas_skipped,
        "continuity_issue_counts": dict(sorted(st.issue_counts.items())),
        "continuity_failures": st.failures(),
        "snapshot_checkpoints": snapshot_log,
        "checkpoint_comparisons": st.checkpoint_comparisons,
        "checkpoint_matches": st.checkpoint_matches,
        "max_bid_levels": scan.max_bid_levels,
        "max_ask_levels": scan.max_ask_levels,
        "final_best_bid": str(top.best_bid) if top else None,
        "final_best_ask": str(top.best_ask) if top else None,
    }
    if has_update_ids:
        out["update_id_diff_distribution"] = {str(k): v for k, v in sorted(u_diff.items())}
        # "+1" is our own strict rule: OBSERVED on the archive, not documented by Bybit.
        out["update_id_plus_one_deltas"] = u_diff.get(1, 0)
        out["update_id_not_plus_one_deltas"] = sum(v for k, v in u_diff.items() if k != 1)
        out["cross_seq_nonincreasing_deltas"] = seq_nonincreasing
        out["first_delta_after_first_snapshot"] = first_delta_after_snapshot
        out["publish_latency_ts_minus_cts_ms"] = _percentiles(latency_ms, (0.0, 0.5, 0.99))
        out["system_ts_regressions"] = system_ts_regressions
    else:
        out["update_id_diff_distribution"] = "NOT AVAILABLE: source publishes no update IDs"
    # Push cadence. Bybit documents 100 ms for level 200. It shows on the publish
    # clock (ts); on the matching-engine clock (cts) consecutive deltas can be a few
    # ms apart, because cts stamps the content of a batch, not its publication.
    qs = (0.0, 0.001, 0.01, 0.05, 0.5)
    out["inter_delta_gap_ms_replay_clock"] = _percentiles(gaps_clock_ms, qs)
    out["inter_delta_gaps_below_95ms_replay_clock"] = sum(1 for g in gaps_clock_ms if g < 95)
    if gaps_publish_ms:
        out["inter_delta_gap_ms_publish_ts"] = _percentiles(gaps_publish_ms, qs)
        out["inter_delta_gaps_below_95ms_publish_ts"] = sum(1 for g in gaps_publish_ms if g < 95)
    return out


def trade_stats(trades: list[TradeEvent], info: TradeFileInfo) -> dict[str, Any]:
    buys = [t for t in trades if t.side is Side.BUY]
    sells = [t for t in trades if t.side is Side.SELL]
    buy_n = sum((t.notional for t in buys), Decimal(0))
    sell_n = sum((t.notional for t in sells), Decimal(0))
    return {
        "rows": info.rows,
        "first_ts_us": trades[0].exchange_ts_us if trades else None,
        "last_ts_us": trades[-1].exchange_ts_us if trades else None,
        "columns": info.columns,
        "first_ts": us_to_iso(trades[0].exchange_ts_us) if trades else None,
        "last_ts": us_to_iso(trades[-1].exchange_ts_us) if trades else None,
        "aggressor_side": (
            "REPORTED by provider; interpreted as taker side (see aggressor_side_vs_book)"
        ),
        "buy_trades": len(buys),
        "sell_trades": len(sells),
        "buy_notional_usdt": str(buy_n),
        "sell_notional_usdt": str(sell_n),
        "file_was_chronological": not info.reordered,
        "out_of_order_rows": info.out_of_order_rows,
        "duplicate_trade_ids": info.duplicate_ids,
        "rpi_trades": info.rpi_trades,
    }


def make_synchronizer(ds: DatasetConfig, cfg: DemoConfig) -> BookSynchronizer:
    book = OrderBook(ds.symbol)
    if ds.provider is ProviderId.BYBIT_ARCHIVE:
        return BookSynchronizer(
            book,
            BybitContinuityValidator(
                ds.symbol, require_contiguous_update_id=cfg.book.require_contiguous_update_id
            ),
        )
    return BookSynchronizer(book, TimestampOnlyContinuityValidator(ds.symbol))


def load_trades(
    ds: DatasetConfig, path: Path, report: ValidationReport
) -> tuple[list[TradeEvent], TradeFileInfo]:
    if ds.provider is ProviderId.BYBIT_ARCHIVE:
        return load_bybit_trades(path, report, symbol=ds.symbol)
    return load_tardis_trades(path, report)


def iter_book(
    ds: DatasetConfig, path: Path, report: ValidationReport, clock: BookClock
) -> Iterable[BookEvent]:
    if ds.provider is ProviderId.BYBIT_ARCHIVE:
        return iter_bybit_orderbook(path, report, book_clock=clock, symbol=ds.symbol)
    return iter_tardis_book(path, report)


def verify_dataset(
    ds: DatasetConfig, cfg: DemoConfig, downloads: list[DownloadRecord]
) -> dict[str, Any]:
    files = {f.kind: f for f in dataset_files(ds, cfg.paths.raw_dir)}
    book_path, trades_path = files[FileKind.BOOK].local_path, files[FileKind.TRADES].local_path
    book_report = ValidationReport(book_path.name)
    trade_report = ValidationReport(trades_path.name)
    sync = make_synchronizer(ds, cfg)
    has_ids = ds.provider is ProviderId.BYBIT_ARCHIVE
    trades, info = load_trades(ds, trades_path, trade_report)
    aggressor = AggressorCheck()
    book_stream = iter_book(ds, book_path, book_report, cfg.replay.book_clock)
    book = verify_book(
        (ev for _, ev in merge_streams(book_stream, trades)),
        sync,
        has_update_ids=has_ids,
        aggressor=aggressor,
    )
    result: dict[str, Any] = {
        "dataset_id": ds.id,
        "provider": ds.provider.value,
        "symbol": ds.symbol,
        "date": ds.date.isoformat(),
        "depth": ds.depth,
        "note": ds.note,
        "files": [d.model_dump(exclude={"from_cache"}) for d in downloads if d.dataset_id == ds.id],
        "book": book,
        "book_schema_validation": book_report.to_dict(),
        "trades": trade_stats(trades, info),
        "trades_schema_validation": trade_report.to_dict(),
        "aggressor_side_vs_book": aggressor.to_dict(),
        "utc_day_bounds": [us_to_iso(x) for x in _day_bounds_us(ds.date)],
    }
    if has_ids:
        result["book_raw_fields"] = raw_bybit_fields(book_path)
    result["verdict"] = _verdict(result, ds)
    return result


def _verdict(r: dict[str, Any], ds: DatasetConfig) -> dict[str, Any]:
    b, tr = r["book"], r["trades"]
    lo, hi = _day_bounds_us(ds.date)
    tol = BOOK_DAY_TOLERANCE // timedelta(microseconds=1)
    agg = r["aggressor_side_vs_book"]
    checks = {
        "book_parsed_without_schema_errors": r["book_schema_validation"]["records_malformed"] == 0,
        "trades_parsed_without_schema_errors": r["trades_schema_validation"]["records_malformed"]
        == 0,
        "has_snapshot": b["snapshots"] >= 1,
        "has_incremental_deltas": b["deltas"] >= 1,
        "no_continuity_failures": b["continuity_failures"] == 0,
        "no_crossed_book": b["continuity_issue_counts"].get("CROSSED_BOOK", 0) == 0,
        "reconstruction_matches_all_checkpoint_snapshots": (
            b["checkpoint_matches"] == b["checkpoint_comparisons"]
        ),
        "trades_present": tr["rows"] > 0,
        "trade_timestamps_inside_utc_day": (
            tr["rows"] > 0 and lo <= tr["first_ts_us"] and tr["last_ts_us"] < hi
        ),
        "book_timestamps_inside_utc_day_plus_minus_60s": (
            b["first_ts_us"] is not None
            and lo - tol <= b["first_ts_us"]
            and b["last_ts_us"] <= hi + tol
        ),
        "reported_side_consistent_with_taker_semantics": (
            agg["taker_consistent_share"] is not None
            and agg["consistent_with_taker_side"] > agg["consistent_with_maker_side"]
        ),
    }
    if ds.depth is not None:
        checks["book_never_exceeds_advertised_depth"] = (
            b["max_bid_levels"] <= ds.depth and b["max_ask_levels"] <= ds.depth
        )
    if r["provider"] == ProviderId.BYBIT_ARCHIVE.value:
        checks["update_id_always_plus_one"] = b["update_id_not_plus_one_deltas"] == 0
        checks["at_least_one_checkpoint_comparison"] = b["checkpoint_comparisons"] >= 1
    return {"checks": checks, "pass": all(checks.values())}


# ------------------------------------------------------------- cross-check
@dataclass(slots=True)
class TopSamples:
    """Top of book at each boundary plus every book-event time (for quiet-mark tests)."""

    tops: dict[int, tuple[Decimal, Decimal]]
    event_times: list[int]

    def quiet(self, mark: int, radius_us: int) -> bool:
        """True if this stream had no book event within ``radius_us`` of ``mark``."""
        i = bisect_left(self.event_times, mark - radius_us)
        return i == len(self.event_times) or self.event_times[i] > mark + radius_us


def sample_top_of_book(
    events: Iterable[BookEvent], sync: BookSynchronizer, interval_us: int
) -> TopSamples:
    """Top of book as of each ``interval`` boundary (state *before* the first event past it).

    Prices are kept as ``Decimal`` so "4.620" (Bybit) and "4.62" (Tardis) compare equal.
    """
    out: dict[int, tuple[Decimal, Decimal]] = {}
    times: list[int] = []
    next_mark: int | None = None
    for ev in events:
        t = ev.exchange_ts_us
        times.append(t)
        if next_mark is None:
            next_mark = (t // interval_us + 1) * interval_us
        while t >= next_mark:
            top = sync.book.top()
            if top is not None and sync.usable:
                out[next_mark] = (top.best_bid, top.best_ask)
            next_mark += interval_us
        sync.process(ev)
    return TopSamples(out, times)


def cross_check(
    a: DatasetConfig, b: DatasetConfig, cfg: DemoConfig, *, interval_s: int = 60
) -> dict[str, Any]:
    """Compare two providers' captures of one symbol/day: trades by ID, top-of-book per minute."""
    fa = {f.kind: f for f in dataset_files(a, cfg.paths.raw_dir)}
    fb = {f.kind: f for f in dataset_files(b, cfg.paths.raw_dir)}
    ta, _ = load_trades(a, fa[FileKind.TRADES].local_path, ValidationReport("a"))
    tb, _ = load_trades(b, fb[FileKind.TRADES].local_path, ValidationReport("b"))
    ia = {t.trade_id: t for t in ta if t.trade_id}
    ib = {t.trade_id: t for t in tb if t.trade_id}
    common = ia.keys() & ib.keys()
    field_mismatch = sum(
        1
        for k in common
        if (ia[k].price, ia[k].quantity, ia[k].side) != (ib[k].price, ib[k].quantity, ib[k].side)
    )
    ts_diff_ms = [abs(ia[k].exchange_ts_us - ib[k].exchange_ts_us) // 1000 for k in common]

    # Tardis book timestamps are the exchange message time ``ts``, so the Bybit
    # side is replayed on its ``ts`` clock here for a like-for-like comparison.
    interval = interval_s * 1_000_000
    sa = sample_top_of_book(
        iter_book(a, fa[FileKind.BOOK].local_path, ValidationReport("a"), "ts"),
        make_synchronizer(a, cfg),
        interval,
    )
    sb = sample_top_of_book(
        iter_book(b, fb[FileKind.BOOK].local_path, ValidationReport("b"), "ts"),
        make_synchronizer(b, cfg),
        interval,
    )
    marks = sorted(sa.tops.keys() & sb.tops.keys())
    equal = [m for m in marks if sa.tops[m] == sb.tops[m]]
    radius = 1_000_000
    quiet = [m for m in marks if sa.quiet(m, radius) and sb.quiet(m, radius)]
    quiet_equal = [m for m in quiet if sa.tops[m] == sb.tops[m]]
    mismatched = [
        {
            "at": us_to_iso(m),
            a.id: [str(x) for x in sa.tops[m]],
            b.id: [str(x) for x in sb.tops[m]],
            "both_streams_quiet_within_1s": m in quiet,
        }
        for m in marks
        if sa.tops[m] != sb.tops[m]
    ][:10]
    return {
        "datasets": [a.id, b.id],
        "trades": {
            f"{a.id}_rows": len(ta),
            f"{b.id}_rows": len(tb),
            "common_trade_ids": len(common),
            f"only_in_{a.id}": len(ia.keys() - ib.keys()),
            f"only_in_{b.id}": len(ib.keys() - ia.keys()),
            "common_ids_with_price_qty_or_side_mismatch": field_mismatch,
            "timestamp_abs_diff_ms": _percentiles(ts_diff_ms, (0.5, 0.99)),
        },
        "top_of_book": {
            "sample_interval_s": interval_s,
            "compared_samples": len(marks),
            "identical_best_bid_and_ask": len(equal),
            "identical_share": round(len(equal) / len(marks), 6) if marks else None,
            "quiet_samples_no_update_in_either_stream_within_1s": len(quiet),
            "quiet_samples_identical": len(quiet_equal),
            "first_mismatches": mismatched,
            "note": (
                "Sampled state as of each boundary. Streams differ in depth and push "
                "cadence (archive ob200 = 100 ms batches), so small timing differences "
                "at a boundary can legitimately produce a mismatch."
            ),
        },
    }
