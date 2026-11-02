"""Transparent WATCH / CANDIDATE / CONFIRMED rule sets.

Research hypotheses for engineering demonstration only -- not strategies
claimed to be profitable. The SHORT rules are the exact sign-mirror of the
LONG rules: :class:`DirectionalView` flips every signed feature and swaps the
support/resistance book sides, and the same thresholds are applied.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bybit_demo.config import CandidateConfig, ConfirmConfig, WatchConfig
from bybit_demo.features.feature_engine import FeatureVector
from bybit_demo.models.signals import Direction


class DirectionalView:
    """Direction-normalized accessors: + always means "in favour of the setup"."""

    __slots__ = ("d", "fv", "s")

    def __init__(self, fv: FeatureVector, direction: Direction) -> None:
        self.fv = fv
        self.d = direction
        self.s = direction.sign

    @property
    def long(self) -> bool:
        return self.d is Direction.LONG

    @property
    def support(self) -> str:
        return "bid" if self.long else "ask"

    @property
    def resistance(self) -> str:
        return "ask" if self.long else "bid"

    @property
    def flow_word(self) -> str:
        return "buy" if self.long else "sell"

    # signed flow
    @property
    def imb_short(self) -> float:
        return self.s * self.fv.imb_short

    @property
    def imb_medium(self) -> float:
        return self.s * self.fv.imb_medium

    @property
    def event_imb(self) -> float:
        return self.s * self.fv.event_imb

    @property
    def acceleration(self) -> float:
        return self.s * self.fv.flow_acceleration

    @property
    def cvd_short(self) -> float:
        return self.s * self.fv.cvd_short_usdt

    @property
    def cvd_medium(self) -> float:
        return self.s * self.fv.cvd_medium_usdt

    @property
    def mid_disp_medium_bps(self) -> float:
        return self.s * self.fv.mid_disp_medium_bps

    # signed book
    @property
    def near_book_imbalance(self) -> float:
        return self.s * self.fv.near_book_imbalance

    @property
    def book_pressure(self) -> float:
        return self.s * self.fv.book_pressure

    @property
    def microprice_rel(self) -> float:
        return self.s * self.fv.microprice_rel

    @property
    def extension_sigma(self) -> float:
        return self.fv.ext_up_sigma if self.long else self.fv.ext_down_sigma

    # support / resistance liquidity
    @property
    def support_withdrawal(self) -> float:
        return self.fv.bid_net_withdrawal if self.long else self.fv.ask_net_withdrawal

    @property
    def resistance_withdrawal(self) -> float:
        return self.fv.ask_net_withdrawal if self.long else self.fv.bid_net_withdrawal

    @property
    def resistance_depletion(self) -> float:
        return self.fv.ask_depletion if self.long else self.fv.bid_depletion

    @property
    def support_replenishment(self) -> float | None:
        return self.fv.bid_replenishment if self.long else self.fv.ask_replenishment

    @property
    def support_absorbed(self) -> bool:
        return self.fv.bid_absorbed if self.long else self.fv.ask_absorbed


@dataclass(slots=True)
class RuleResult:
    """Outcome of one rule stage: which named conditions passed / failed."""

    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed

    def check(self, condition: bool, name: str) -> bool:
        (self.passed if condition else self.failed).append(name)
        return condition


def core_flow(v: DirectionalView, w: WatchConfig) -> RuleResult:
    """Flow conditions whose persistence keeps a WATCH alive."""
    r = RuleResult()
    r.check(v.imb_medium >= w.min_flow_imbalance, f"aggressive_{v.flow_word}_imbalance")
    r.check(v.acceleration >= w.min_flow_acceleration, f"{v.flow_word}_flow_accelerating")
    r.check(v.cvd_short > 0 and v.cvd_medium > 0, "rolling_cvd_strengthening")
    return r


def watch_structure(v: DirectionalView, w: WatchConfig) -> RuleResult:
    r = RuleResult()
    cr = v.fv.compression_ratio
    r.check(cr is not None and cr <= w.max_compression_ratio, "price_compressed")
    r.check(v.support_withdrawal <= w.max_support_withdrawal, f"{v.support}_support_holding")
    if v.near_book_imbalance >= w.min_book_imbalance:
        r.passed.append(f"near_touch_book_imbalance_{v.flow_word}")
    elif v.resistance_withdrawal >= w.min_resistance_withdrawal:
        r.passed.append(f"{v.resistance}_liquidity_weakening")
    else:
        r.failed.append(f"{v.resistance}_resistance_not_weakening")
    return r


def candidate_rules(
    v: DirectionalView,
    c: CandidateConfig,
    w: WatchConfig,
    *,
    watch_duration_ms: float,
    trades_in_watch: int,
) -> RuleResult:
    r = RuleResult()
    r.check(
        watch_duration_ms >= c.min_watch_duration_ms and trades_in_watch >= c.min_trades_in_watch,
        f"persistent_{v.flow_word}_flow",
    )
    r.check(v.imb_medium >= c.min_flow_imbalance, f"strong_{v.flow_word}_imbalance")
    r.check(v.event_imb >= c.min_event_flow_imbalance, "event_window_cvd_agrees")
    if v.book_pressure >= c.min_book_pressure:
        r.passed.append(f"book_pressure_{v.flow_word}")
    elif v.resistance_withdrawal >= c.min_resistance_withdrawal:
        r.passed.append(f"{v.resistance}_withdrawal")
    else:
        r.failed.append("no_book_pressure_or_withdrawal")
    repl = v.support_replenishment
    if v.support_absorbed:
        r.passed.append(f"{v.support}_absorption")
    elif repl is not None and repl >= c.min_support_replenishment:
        r.passed.append(f"{v.support}_replenishment")
    elif repl is None and v.support_withdrawal <= w.max_support_withdrawal:
        r.passed.append(f"{v.support}_support_unchallenged")
    else:
        r.failed.append(f"{v.support}_support_not_confirmed")
    return r


def confirm_rules(v: DirectionalView, f: ConfirmConfig) -> RuleResult:
    r = RuleResult()
    r.check(
        v.imb_short >= f.min_flow_imbalance_short and v.cvd_short > 0,
        f"{v.flow_word}_flow_continuation",
    )
    r.check(
        v.resistance_depletion >= f.min_resistance_depletion,
        f"{v.resistance}_liquidity_consumed_or_withdrawn",
    )
    r.check(
        v.microprice_rel >= f.min_microprice_rel and v.book_pressure >= f.min_book_pressure,
        "microprice_and_book_pressure_aligned",
    )
    return r
