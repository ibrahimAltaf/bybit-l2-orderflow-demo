"""Point-in-time feature vector, from state already consumed on the replay clock.

The engine is fed events one at a time by the replay engine. ``compute(now)``
reads only accumulators that have already absorbed events with time <= now,
so a feature vector can never depend on a later event.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal

from bybit_demo.book.continuity import BookUpdate
from bybit_demo.book.metrics import LiquidityFlowTracker, compute_book_metrics
from bybit_demo.book.reconstruction import OrderBook
from bybit_demo.config import FeatureConfig, GateConfig
from bybit_demo.features.absorption import measure_absorption
from bybit_demo.features.liquidity import book_pressure, min_side_depth, spread_regime
from bybit_demo.features.volatility import VolatilityTracker
from bybit_demo.flow.cvd import CVDTracker, EventFlowWindow, FlowWindow
from bybit_demo.flow.rolling import LaggedValue
from bybit_demo.models.events import TradeEvent
from bybit_demo.models.orderbook import BookSide

_US = 1_000_000


@dataclass(frozen=True, slots=True)
class FeatureVector:
    """All features at one instant. Flow/imbalance values: + = buy side."""

    ts_us: int
    event_index: int
    # integrity
    book_usable: bool
    synced: bool
    crossed: bool
    book_age_ms: float
    elapsed_s: float  # since first usable book
    update_id: int | None
    cross_seq: int | None
    # top of book
    best_bid: str
    best_ask: str
    mid: float
    spread_bps: float
    spread_regime: str
    microprice_disp_bps: float
    microprice_rel: float
    # depth / pressure
    near_book_imbalance: float
    book_pressure: float
    gate_depth_min_usdt: float  # thinner side within gate band
    depth_bid_usdt: float  # within gate band
    depth_ask_usdt: float
    # trade flow
    imb_short: float
    imb_medium: float
    imb_long: float
    cvd_short_usdt: float
    cvd_medium_usdt: float
    cvd_long_usdt: float
    count_imb_short: float
    event_imb: float
    flow_acceleration: float  # imb_short - imb_long
    trades_short: int
    trades_medium: int
    largest_trade_share_short: float
    cvd_session_usdt: float
    cvd_session_qty: float
    session_trades: int
    # liquidity flow (liquidity band, liquidity window)
    bid_net_withdrawal: float
    ask_net_withdrawal: float
    bid_depletion: float
    ask_depletion: float
    bid_replenishment: float | None
    ask_replenishment: float | None
    bid_add_rate: float
    bid_cancel_rate: float
    ask_add_rate: float
    ask_cancel_rate: float
    # absorption
    bid_absorbed: bool
    ask_absorbed: bool
    bid_absorption_score: float
    ask_absorption_score: float
    # volatility / extension
    rv_short_bps: float
    rv_long_bps: float
    compression_ratio: float | None
    ext_up_sigma: float
    ext_down_sigma: float
    ext_up_bps: float
    ext_down_bps: float
    mid_disp_medium_bps: float

    def to_dict(self) -> dict[str, float | int | str | None]:
        out: dict[str, float | int | str | None] = {}
        for k, v in asdict(self).items():
            if isinstance(v, bool):
                out[k] = int(v)
            elif isinstance(v, float):
                out[k] = round(v, 6)
            else:
                out[k] = v
        return out


class FeatureEngine:
    def __init__(self, cfg: FeatureConfig, gates: GateConfig) -> None:
        self.cfg = cfg
        self.gates = gates
        s, m, lg = cfg.flow_windows_s
        self._win_s, self._win_m, self._win_l = s, m, lg
        self.cvd = CVDTracker()
        self.flow_short = FlowWindow(int(s * _US))
        self.flow_medium = FlowWindow(int(m * _US))
        self.flow_long = FlowWindow(int(lg * _US))
        self.event_flow = EventFlowWindow(cfg.event_window_trades)
        self.absorb_flow = FlowWindow(int(cfg.absorption_window_s * _US))
        self.liquidity = LiquidityFlowTracker(
            cfg.liquidity_band_bps,
            int(cfg.liquidity_window_s * _US),
            int(cfg.replenish_memory_s * _US),
        )
        self.vol = VolatilityTracker(
            cfg.vol_short_s, cfg.vol_long_s, cfg.extension_window_s, cfg.min_sigma_bps
        )
        self._mid_lag_medium = LaggedValue(int(m * _US))
        self._mid_lag_absorb = LaggedValue(int(cfg.absorption_window_s * _US))
        # Configured bands first (book pressure uses them positionally), then any
        # extra band needed by the gate / liquidity accounting, all in one book walk.
        bands = list(cfg.depth_bands_bps)
        for extra in (gates.depth_band_bps, cfg.liquidity_band_bps):
            if extra not in bands:
                bands.append(extra)
        self._bands = tuple(bands)
        self._first_usable_us: int | None = None
        self._last_book_us: int | None = None

    # ------------------------------------------------------------ ingestion
    def on_trade(self, trade: TradeEvent) -> None:
        self.cvd.on_trade(trade)
        for w in (self.flow_short, self.flow_medium, self.flow_long, self.absorb_flow):
            w.on_trade(trade)
        self.event_flow.on_trade(trade)
        self.liquidity.on_trade(trade)

    def on_book(self, now_us: int, update: BookUpdate, book: OrderBook, usable: bool) -> None:
        if not update.applied:
            return
        self._last_book_us = now_us
        top = book.top()
        mid = top.mid if top is not None else None
        self.liquidity.on_book_changes(now_us, update.changes, mid)
        if usable and mid is not None:
            if self._first_usable_us is None:
                self._first_usable_us = now_us
            self.vol.on_mid(now_us, mid)
            self._mid_lag_medium.add(now_us, mid)
            self._mid_lag_absorb.add(now_us, mid)

    # ------------------------------------------------------------ features
    def compute(
        self,
        now_us: int,
        event_index: int,
        book: OrderBook,
        *,
        synced: bool,
        crossed: bool,
    ) -> FeatureVector | None:
        # An invalidated book (sequence gap) still holds its last levels: keep computing
        # so the detector sees synced=False and rejects explicitly (SEQUENCE_GAP).
        metrics = compute_book_metrics(book, self._bands) if book.snapshots_applied else None
        if metrics is None:
            return None
        for w in (self.flow_short, self.flow_medium, self.flow_long, self.absorb_flow):
            w.advance(now_us)
        cfg = self.cfg
        mid = metrics.mid
        gate_band = metrics.band(self.gates.depth_band_bps)
        liq_band = metrics.band(cfg.liquidity_band_bps)
        liq = self.liquidity.snapshot(now_us)
        bid_flow, ask_flow = liq[BookSide.BID], liq[BookSide.ASK]
        vol = self.vol.snapshot(now_us, mid)

        lag_m = self._mid_lag_medium.value_at_lag(now_us)
        disp_m = 0.0 if lag_m is None else float((mid - lag_m) / lag_m) * 1e4
        lag_a = self._mid_lag_absorb.value_at_lag(now_us)
        disp_a = 0.0 if lag_a is None else float((mid - lag_a) / lag_a) * 1e4

        # Typical per-side notional for one absorption window, from the long window.
        scale = Decimal(str(cfg.absorption_window_s / self._win_l / 2))
        typical = self.flow_long.total_notional * scale
        bid_abs = measure_absorption(
            self.absorb_flow.sell.total,
            typical,
            -disp_a,
            min_volume_vs_typical=cfg.absorption_min_volume_vs_typical,
            max_displacement_bps=cfg.absorption_max_displacement_bps,
        )
        ask_abs = measure_absorption(
            self.absorb_flow.buy.total,
            typical,
            disp_a,
            min_volume_vs_typical=cfg.absorption_min_volume_vs_typical,
            max_displacement_bps=cfg.absorption_max_displacement_bps,
        )

        short_total = self.flow_short.total_notional
        largest_share = (
            0.0 if short_total == 0 else float(self.flow_short.largest_trade() / short_total)
        )

        def depletion(
            flow_exec: Decimal, flow_cancel: Decimal, flow_add: Decimal, rest: Decimal
        ) -> float:
            base = rest if rest > 0 else Decimal(1)
            return float((flow_exec + flow_cancel - flow_add) / base)

        near = metrics.band(cfg.near_band_bps)
        top = metrics.top
        book_age_ms = 0.0 if self._last_book_us is None else (now_us - self._last_book_us) / 1000
        elapsed = 0.0 if self._first_usable_us is None else (now_us - self._first_usable_us) / _US
        return FeatureVector(
            ts_us=now_us,
            event_index=event_index,
            book_usable=synced and not crossed,
            synced=synced,
            crossed=crossed,
            book_age_ms=book_age_ms,
            elapsed_s=elapsed,
            update_id=book.last_update_id,
            cross_seq=book.last_cross_seq,
            best_bid=str(top.best_bid),
            best_ask=str(top.best_ask),
            mid=float(mid),
            spread_bps=metrics.spread_bps,
            spread_regime=spread_regime(
                metrics.spread_bps, self.gates.tight_spread_bps, self.gates.max_spread_bps
            ).value,
            microprice_disp_bps=metrics.microprice_disp_bps,
            microprice_rel=metrics.microprice_rel,
            near_book_imbalance=near.imbalance,
            book_pressure=book_pressure(
                metrics.bands[: len(cfg.book_pressure_weights)], cfg.book_pressure_weights
            ),
            gate_depth_min_usdt=min_side_depth(gate_band),
            depth_bid_usdt=float(gate_band.bid_notional),
            depth_ask_usdt=float(gate_band.ask_notional),
            imb_short=self.flow_short.imbalance,
            imb_medium=self.flow_medium.imbalance,
            imb_long=self.flow_long.imbalance,
            cvd_short_usdt=float(self.flow_short.signed_notional),
            cvd_medium_usdt=float(self.flow_medium.signed_notional),
            cvd_long_usdt=float(self.flow_long.signed_notional),
            count_imb_short=self.flow_short.count_imbalance,
            event_imb=self.event_flow.imbalance,
            flow_acceleration=self.flow_short.imbalance - self.flow_long.imbalance,
            trades_short=self.flow_short.trade_count,
            trades_medium=self.flow_medium.trade_count,
            largest_trade_share_short=largest_share,
            cvd_session_usdt=float(self.cvd.cvd_notional),
            cvd_session_qty=float(self.cvd.cvd_qty),
            session_trades=self.cvd.trade_count,
            bid_net_withdrawal=bid_flow.net_withdrawal_ratio(liq_band.bid_notional),
            ask_net_withdrawal=ask_flow.net_withdrawal_ratio(liq_band.ask_notional),
            bid_depletion=depletion(
                bid_flow.executed, bid_flow.cancelled, bid_flow.added, liq_band.bid_notional
            ),
            ask_depletion=depletion(
                ask_flow.executed, ask_flow.cancelled, ask_flow.added, liq_band.ask_notional
            ),
            bid_replenishment=bid_flow.replenishment_ratio,
            ask_replenishment=ask_flow.replenishment_ratio,
            bid_add_rate=bid_flow.add_rate,
            bid_cancel_rate=bid_flow.cancel_rate,
            ask_add_rate=ask_flow.add_rate,
            ask_cancel_rate=ask_flow.cancel_rate,
            bid_absorbed=bid_abs.absorbed,
            ask_absorbed=ask_abs.absorbed,
            bid_absorption_score=bid_abs.score,
            ask_absorption_score=ask_abs.score,
            rv_short_bps=vol.rv_short_bps,
            rv_long_bps=vol.rv_long_bps,
            compression_ratio=vol.compression_ratio,
            ext_up_sigma=vol.ext_up_sigma,
            ext_down_sigma=vol.ext_down_sigma,
            ext_up_bps=vol.ext_up_bps,
            ext_down_bps=vol.ext_down_bps,
            mid_disp_medium_bps=disp_m,
        )
