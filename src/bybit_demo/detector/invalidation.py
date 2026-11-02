"""Explicit rejection checks.

* :func:`gate_failures` -- data-integrity and execution-liquidity gates that
  apply to every state (SEQUENCE_GAP, CROSSED_BOOK, STALE_BOOK,
  INSUFFICIENT_HISTORY, SPREAD_TOO_WIDE, INSUFFICIENT_DEPTH).
* :func:`setup_invalidations` -- reasons an *active* setup is abandoned
  (FLOW_REVERSAL, BOOK_PRESSURE_REVERSAL, CVD_DIVERGENCE).
* :func:`spike_check` -- ISOLATED_SPIKE: the flow is one print, not a process.
* :func:`extension_check` -- ALREADY_EXTENDED: too late to be "early".
"""

from __future__ import annotations

from bybit_demo.config import GateConfig, InvalidationConfig
from bybit_demo.detector.rules import DirectionalView
from bybit_demo.features.feature_engine import FeatureVector
from bybit_demo.models.signals import RejectionReason as R


def gate_failures(fv: FeatureVector, g: GateConfig) -> list[R]:
    out: list[R] = []
    if not fv.synced:
        out.append(R.SEQUENCE_GAP)
    if fv.crossed:
        out.append(R.CROSSED_BOOK)
    if fv.book_age_ms > g.max_book_age_ms:
        out.append(R.STALE_BOOK)
    if fv.elapsed_s < g.warmup_s:
        out.append(R.INSUFFICIENT_HISTORY)
    if fv.spread_bps > g.max_spread_bps:
        out.append(R.SPREAD_TOO_WIDE)
    if fv.gate_depth_min_usdt < g.min_depth_usdt:
        out.append(R.INSUFFICIENT_DEPTH)
    return out


def setup_invalidations(v: DirectionalView, inv: InvalidationConfig) -> list[R]:
    out: list[R] = []
    if v.imb_short <= -inv.flow_reversal_imbalance:
        out.append(R.FLOW_REVERSAL)
    if v.book_pressure <= -inv.book_pressure_reversal:
        out.append(R.BOOK_PRESSURE_REVERSAL)
    if v.cvd_medium > 0 and v.mid_disp_medium_bps <= -inv.cvd_divergence_bps:
        out.append(R.CVD_DIVERGENCE)
    return out


def spike_check(fv: FeatureVector, inv: InvalidationConfig) -> list[R]:
    if (
        fv.largest_trade_share_short >= inv.isolated_spike_share
        or fv.trades_short < inv.min_trades_short_window
    ):
        return [R.ISOLATED_SPIKE]
    return []


def extension_check(v: DirectionalView, max_sigma: float) -> list[R]:
    return [R.ALREADY_EXTENDED] if v.extension_sigma > max_sigma else []
