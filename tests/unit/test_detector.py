from __future__ import annotations

import pytest

from bybit_demo.detector.engine import DetectorEngine
from bybit_demo.features.feature_engine import FeatureVector
from bybit_demo.models.signals import Direction
from bybit_demo.models.signals import RejectionReason as R
from bybit_demo.models.signals import SignalState as S
from tests.helpers import demo_config, make_fv, mirror

MS = 1_000
T0 = 10_000_000_000


@pytest.fixture
def det() -> DetectorEngine:
    return DetectorEngine("TESTUSDT", demo_config().detector, directions=(Direction.LONG,))


def at(ms: float, **kw: object) -> FeatureVector:
    """Passing LONG vector at T0 + ms, with session trades growing 5 per 100 ms."""
    return make_fv(ts_us=T0 + int(ms * MS), session_trades=1000 + int(ms // 20), **kw)


def drive_to_candidate(det: DetectorEngine) -> None:
    det.evaluate(at(0))
    assert det.state(Direction.LONG) is S.WATCH
    for ms in range(100, 3100, 100):
        det.evaluate(at(ms, ask_depletion=0.0))  # hold confirmation back
    assert det.state(Direction.LONG) is S.CANDIDATE


def test_idle_to_watch(det: DetectorEngine) -> None:
    (rec,) = det.evaluate(at(0))
    assert (rec.previous_state, rec.new_state) == (S.IDLE, S.WATCH)
    assert "aggressive_buy_imbalance" in rec.reasons
    assert "price_compressed" in rec.reasons
    assert rec.thresholds["watch.min_flow_imbalance"] == 0.2
    assert rec.features["imb_medium"] == 0.4


def test_no_watch_without_flow(det: DetectorEngine) -> None:
    assert det.evaluate(at(0, imb_medium=0.05)) == []
    assert det.state(Direction.LONG) is S.IDLE


def test_no_watch_when_not_compressed(det: DetectorEngine) -> None:
    assert det.evaluate(at(0, compression_ratio=2.0)) == []
    assert det.evaluate(at(1, compression_ratio=None)) == []


def test_watch_to_candidate_requires_persistence(det: DetectorEngine) -> None:
    det.evaluate(at(0))
    assert det.evaluate(at(1000)) == []  # 1 s < min_watch_duration_ms
    recs = []
    for ms in range(1100, 3100, 100):
        recs += det.evaluate(at(ms, ask_depletion=0.0))
    (rec,) = recs
    assert rec.new_state is S.CANDIDATE
    assert rec.ts_us == T0 + 3000 * MS
    assert {"persistent_buy_flow", "bid_replenishment", "liquidity_gate_pass"} <= set(rec.reasons)


def test_candidate_to_confirmed_has_signal_timestamp(det: DetectorEngine) -> None:
    drive_to_candidate(det)
    (rec,) = det.evaluate(at(3200))
    assert (rec.previous_state, rec.new_state) == (S.CANDIDATE, S.CONFIRMED)
    d = rec.to_dict()
    assert d["signal_timestamp"] == d["timestamp"]
    assert "ask_liquidity_consumed_or_withdrawn" in rec.reasons


def test_confirmed_then_cooldown_then_idle(det: DetectorEngine) -> None:
    drive_to_candidate(det)
    det.evaluate(at(3200))
    assert det.evaluate(at(30_000)) == []  # still cooling down
    (rec,) = det.evaluate(at(63_200))
    assert (rec.previous_state, rec.new_state) == (S.CONFIRMED, S.IDLE)


def test_flow_reversal_invalidates_watch(det: DetectorEngine) -> None:
    det.evaluate(at(0))
    (rec,) = det.evaluate(at(500, imb_short=-0.5))
    assert rec.new_state is S.REJECTED
    assert R.FLOW_REVERSAL in rec.rejection_flags


def test_book_pressure_reversal_and_cvd_divergence(det: DetectorEngine) -> None:
    det.evaluate(at(0))
    (rec,) = det.evaluate(at(500, book_pressure=-0.5, mid_disp_medium_bps=-6.0))
    assert {R.BOOK_PRESSURE_REVERSAL, R.CVD_DIVERGENCE} <= set(rec.rejection_flags)


def test_late_entry_rejected_at_idle(det: DetectorEngine) -> None:
    (rec,) = det.evaluate(at(0, ext_up_sigma=3.0))
    assert (rec.previous_state, rec.new_state) == (S.IDLE, S.REJECTED)
    assert rec.rejection_flags == (R.ALREADY_EXTENDED,)


def test_late_entry_rejected_at_confirmation(det: DetectorEngine) -> None:
    drive_to_candidate(det)
    (rec,) = det.evaluate(at(3200, ext_up_sigma=2.6))  # within WATCH cap? no: > confirm cap 2.5
    assert rec.new_state is S.REJECTED and R.ALREADY_EXTENDED in rec.rejection_flags


@pytest.mark.parametrize(
    ("override", "flag"),
    [
        ({"spread_bps": 12.0}, R.SPREAD_TOO_WIDE),
        ({"gate_depth_min_usdt": 500.0}, R.INSUFFICIENT_DEPTH),
        ({"book_age_ms": 9_000.0}, R.STALE_BOOK),
        ({"elapsed_s": 10.0}, R.INSUFFICIENT_HISTORY),
        ({"crossed": True}, R.CROSSED_BOOK),
        ({"synced": False}, R.SEQUENCE_GAP),
    ],
)
def test_gates_reject_setup_at_idle(
    det: DetectorEngine, override: dict[str, object], flag: R
) -> None:
    (rec,) = det.evaluate(at(0, **override))
    assert rec.new_state is S.REJECTED and flag in rec.rejection_flags


def test_liquidity_and_sequence_gate_reject_active_candidate(det: DetectorEngine) -> None:
    drive_to_candidate(det)
    (rec,) = det.evaluate(at(3200, synced=False, gate_depth_min_usdt=10.0))
    assert rec.previous_state is S.CANDIDATE
    assert {R.SEQUENCE_GAP, R.INSUFFICIENT_DEPTH} <= set(rec.rejection_flags)


def test_isolated_spike_rejected(det: DetectorEngine) -> None:
    (rec,) = det.evaluate(at(0, largest_trade_share_short=0.9))
    assert rec.rejection_flags == (R.ISOLATED_SPIKE,)
    det2 = DetectorEngine("T", demo_config().detector, directions=(Direction.LONG,))
    (rec2,) = det2.evaluate(at(0, trades_short=1))
    assert rec2.rejection_flags == (R.ISOLATED_SPIKE,)


def test_candidate_timeout(det: DetectorEngine) -> None:
    drive_to_candidate(det)
    recs = []
    for ms in range(3100, 34_000, 500):
        recs += det.evaluate(at(ms, ask_depletion=0.0))
    (rec,) = recs
    assert rec.rejection_flags == (R.CANDIDATE_TIMEOUT,)


def test_watch_lapses_back_to_idle(det: DetectorEngine) -> None:
    det.evaluate(at(0))
    assert det.evaluate(at(1000, imb_medium=0.1)) == []
    (rec,) = det.evaluate(at(2100, imb_medium=0.1))
    assert (rec.previous_state, rec.new_state) == (S.WATCH, S.IDLE)
    assert "watch_lapsed" in rec.reasons


def test_rejection_cooldown_then_idle(det: DetectorEngine) -> None:
    det.evaluate(at(0, spread_bps=12.0))
    assert det.evaluate(at(5_000)) == []
    (rec,) = det.evaluate(at(10_000))
    assert (rec.previous_state, rec.new_state) == (S.REJECTED, S.IDLE)


def test_short_is_exact_mirror_of_long() -> None:
    cfg = demo_config().detector
    long_det = DetectorEngine("T", cfg)
    short_det = DetectorEngine("T", cfg)
    long_states, short_states = [], []
    for ms in [0, *range(100, 3300, 100)]:
        fv = at(ms, ask_depletion=0.0 if ms < 3200 else 0.4)
        long_states += [(r.direction, r.new_state) for r in long_det.evaluate(fv)]
        short_states += [(r.direction, r.new_state) for r in short_det.evaluate(mirror(fv))]
    assert [s for d, s in long_states if d is Direction.LONG] == [S.WATCH, S.CANDIDATE, S.CONFIRMED]
    assert [s for d, s in short_states if d is Direction.SHORT] == [
        S.WATCH,
        S.CANDIDATE,
        S.CONFIRMED,
    ]
    assert not [s for d, s in long_states if d is Direction.SHORT]
    assert not [s for d, s in short_states if d is Direction.LONG]


def test_every_transition_record_is_complete(det: DetectorEngine) -> None:
    drive_to_candidate(det)
    (rec,) = det.evaluate(at(3200))
    d = rec.to_dict()
    for key in (
        "timestamp",
        "symbol",
        "previous_state",
        "new_state",
        "direction",
        "features",
        "thresholds",
        "reasons",
        "rejection_flags",
        "update_id",
        "cross_seq",
    ):
        assert key in d
    for feat in (
        "best_bid",
        "best_ask",
        "spread_bps",
        "gate_depth_min_usdt",
        "cvd_short_usdt",
        "imb_medium",
        "near_book_imbalance",
        "ext_up_sigma",
    ):
        assert feat in d["features"]
