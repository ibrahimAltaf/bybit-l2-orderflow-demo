"""Shared test helpers (no market data here)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from bybit_demo.audit.logger import MemorySink
from bybit_demo.book.continuity import BookSynchronizer, BybitContinuityValidator
from bybit_demo.book.reconstruction import OrderBook
from bybit_demo.config import DemoConfig, load_config
from bybit_demo.detector.engine import DetectorEngine
from bybit_demo.features.feature_engine import FeatureEngine, FeatureVector
from bybit_demo.models.events import (
    AggressorSource,
    BookDelta,
    BookSnapshot,
    MarketEvent,
    PriceLevel,
    Side,
    TradeEvent,
)
from bybit_demo.replay.clock import ReplayClock
from bybit_demo.replay.engine import ReplayEngine, ReplaySummary

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "demo.yaml"


def demo_config() -> DemoConfig:
    return load_config(CONFIG_PATH)


def D(x: str | int) -> Decimal:
    return Decimal(str(x))


def lv(p: str, q: str) -> PriceLevel:
    return PriceLevel(D(p), D(q))


def snap(
    u: int,
    bids: list[tuple[str, str]],
    asks: list[tuple[str, str]],
    *,
    ts: int = 1_000,
    seq: int | None = None,
    symbol: str = "TESTUSDT",
) -> BookSnapshot:
    return BookSnapshot(
        exchange="bybit",
        symbol=symbol,
        exchange_ts_us=ts,
        bids=tuple(lv(p, q) for p, q in bids),
        asks=tuple(lv(p, q) for p, q in asks),
        update_id=u,
        cross_seq=seq if seq is not None else u * 10,
    )


def delta(
    u: int,
    bids: Sequence[tuple[str, str]] = (),
    asks: Sequence[tuple[str, str]] = (),
    *,
    ts: int = 2_000,
    seq: int | None = None,
    symbol: str = "TESTUSDT",
) -> BookDelta:
    return BookDelta(
        exchange="bybit",
        symbol=symbol,
        exchange_ts_us=ts,
        bid_updates=tuple(lv(p, q) for p, q in bids),
        ask_updates=tuple(lv(p, q) for p, q in asks),
        update_id=u,
        cross_seq=seq if seq is not None else u * 10,
    )


def trade(
    ts: int, side: Side, price: str, qty: str, *, tid: str | None = None, rpi: bool = False
) -> TradeEvent:
    return TradeEvent(
        exchange="bybit",
        symbol="TESTUSDT",
        exchange_ts_us=ts,
        price=D(price),
        quantity=D(qty),
        side=side,
        aggressor_source=AggressorSource.REPORTED,
        trade_id=tid,
        raw={"RPI": "1"} if rpi else {},
    )


# A feature vector in which every LONG WATCH/CANDIDATE/CONFIRM condition holds
# and no gate or invalidation fires (values chosen relative to config/demo.yaml).
_PASSING: dict[str, Any] = dict(
    ts_us=10_000_000_000,
    event_index=0,
    book_usable=True,
    synced=True,
    crossed=False,
    book_age_ms=50.0,
    elapsed_s=1_000.0,
    update_id=1,
    cross_seq=10,
    best_bid="99.99",
    best_ask="100.01",
    mid=100.0,
    spread_bps=2.0,
    spread_regime="TIGHT",
    microprice_disp_bps=0.2,
    microprice_rel=0.2,
    near_book_imbalance=0.2,
    book_pressure=0.2,
    gate_depth_min_usdt=50_000.0,
    depth_bid_usdt=60_000.0,
    depth_ask_usdt=50_000.0,
    imb_short=0.6,
    imb_medium=0.4,
    imb_long=0.1,
    cvd_short_usdt=2_000.0,
    cvd_medium_usdt=5_000.0,
    cvd_long_usdt=6_000.0,
    count_imb_short=0.5,
    event_imb=0.4,
    flow_acceleration=0.5,
    trades_short=20,
    trades_medium=100,
    largest_trade_share_short=0.1,
    cvd_session_usdt=10_000.0,
    cvd_session_qty=100.0,
    session_trades=1_000,
    bid_net_withdrawal=-0.1,
    ask_net_withdrawal=0.2,
    bid_depletion=0.0,
    ask_depletion=0.4,
    bid_replenishment=0.8,
    ask_replenishment=None,
    bid_add_rate=10.0,
    bid_cancel_rate=0.0,
    ask_add_rate=0.0,
    ask_cancel_rate=10.0,
    bid_absorbed=False,
    ask_absorbed=False,
    bid_absorption_score=0.0,
    ask_absorption_score=0.0,
    rv_short_bps=1.0,
    rv_long_bps=5.0,
    compression_ratio=0.5,
    ext_up_sigma=0.5,
    ext_down_sigma=0.0,
    ext_up_bps=1.0,
    ext_down_bps=0.0,
    mid_disp_medium_bps=1.0,
)


def make_fv(**overrides: Any) -> FeatureVector:
    return replace(FeatureVector(**_PASSING), **overrides)


def mirror(fv: FeatureVector) -> FeatureVector:
    """Sign-flip a LONG-passing vector into the equivalent SHORT-passing one."""
    signed = (
        "imb_short",
        "imb_medium",
        "imb_long",
        "cvd_short_usdt",
        "cvd_medium_usdt",
        "cvd_long_usdt",
        "count_imb_short",
        "event_imb",
        "flow_acceleration",
        "near_book_imbalance",
        "book_pressure",
        "microprice_rel",
        "microprice_disp_bps",
        "mid_disp_medium_bps",
    )
    swaps = (
        ("bid_net_withdrawal", "ask_net_withdrawal"),
        ("bid_depletion", "ask_depletion"),
        ("bid_replenishment", "ask_replenishment"),
        ("bid_absorbed", "ask_absorbed"),
        ("ext_up_sigma", "ext_down_sigma"),
        ("ext_up_bps", "ext_down_bps"),
    )
    upd: dict[str, Any] = {k: -getattr(fv, k) for k in signed}
    for a, b in swaps:
        upd[a], upd[b] = getattr(fv, b), getattr(fv, a)
    return replace(fv, **upd)


def run_events(
    streams: Iterable[Iterable[MarketEvent]],
    cfg: DemoConfig | None = None,
    symbol: str = "SYNTHUSDT",
    **run_kwargs: Any,
) -> tuple[ReplaySummary, MemorySink]:
    cfg = cfg or demo_config()
    sink = MemorySink()
    eng = ReplayEngine(
        symbol,
        synchronizer=BookSynchronizer(OrderBook(symbol), BybitContinuityValidator(symbol)),
        features=FeatureEngine(cfg.features, cfg.detector.gates),
        detector=DetectorEngine(symbol, cfg.detector),
        clock=ReplayClock(),
        sinks=[sink],
    )
    return eng.run(*streams, **run_kwargs), sink
