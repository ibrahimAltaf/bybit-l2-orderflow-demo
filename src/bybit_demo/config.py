"""Typed configuration loaded from YAML (config/demo.yaml).

No threshold lives in code: every rule parameter is a field here.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from bybit_demo.errors import ConfigError


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderId(StrEnum):
    BYBIT_ARCHIVE = "bybit_archive"  # quote-saver.bycsi.com orderbook + public.bybit.com trades
    TARDIS_FREE = "tardis_free"  # datasets.tardis.dev first-day-of-month CSV


class DatasetConfig(_Strict):
    id: str
    provider: ProviderId
    symbol: str
    date: date
    depth: int | None = None  # Bybit archive depth (500 before ~2025-08-21, 200 after)
    role: Literal["primary", "verify", "cross_check"] = "verify"
    note: str = ""


class PathsConfig(_Strict):
    raw_dir: Path = Path("data/raw")
    evidence_dir: Path = Path("data/evidence")
    synthetic_output_dir: Path = Path("data/synthetic_demo_output")


class ReplayConfig(_Strict):
    # Which Bybit book timestamp orders book events against trades:
    #  "cts" = matching-engine time (same clock as trade ``T``)  [default]
    #  "ts"  = publish time (later than cts by 0-399 ms on the verified sample)
    book_clock: Literal["cts", "ts"] = "cts"
    speed: float = Field(0.0, ge=0.0)  # 0 = fast deterministic; 1.0 = real time; 10 = 10x
    max_events: int | None = Field(None, gt=0)
    start_utc: str | None = None  # optional ISO-8601 window, applied causally (filter only)
    end_utc: str | None = None


class FeatureConfig(_Strict):
    depth_bands_bps: tuple[float, ...] = (10.0, 25.0, 50.0)
    book_pressure_weights: tuple[float, ...] = (0.5, 0.3, 0.2)
    near_band_bps: float = 10.0
    flow_windows_s: tuple[float, float, float] = (5.0, 30.0, 120.0)  # short, medium, long
    event_window_trades: int = Field(50, gt=0)
    liquidity_band_bps: float = 25.0
    liquidity_window_s: float = 10.0
    replenish_memory_s: float = 5.0
    vol_short_s: float = 30.0
    vol_long_s: float = 300.0
    extension_window_s: float = 120.0
    min_sigma_bps: float = 3.0
    absorption_window_s: float = 30.0
    absorption_min_volume_vs_typical: float = 1.5
    absorption_max_displacement_bps: float = 3.0

    @model_validator(mode="after")
    def _check(self) -> FeatureConfig:
        if len(self.book_pressure_weights) != len(self.depth_bands_bps):
            raise ValueError("book_pressure_weights must align with depth_bands_bps")
        if self.near_band_bps not in self.depth_bands_bps:
            raise ValueError("near_band_bps must be one of depth_bands_bps")
        s, m, lg = self.flow_windows_s
        if not 0 < s < m < lg:
            raise ValueError("flow_windows_s must be strictly increasing")
        return self


class GateConfig(_Strict):
    max_spread_bps: float
    tight_spread_bps: float
    depth_band_bps: float
    reference_order_notional_usdt: float
    min_depth_multiple: float
    max_book_age_ms: int
    warmup_s: float

    @property
    def min_depth_usdt(self) -> float:
        return self.reference_order_notional_usdt * self.min_depth_multiple


class WatchConfig(_Strict):
    max_compression_ratio: float
    min_flow_imbalance: float
    min_flow_acceleration: float
    max_support_withdrawal: float
    min_book_imbalance: float
    min_resistance_withdrawal: float
    max_extension_sigma: float
    lapse_grace_ms: int


class CandidateConfig(_Strict):
    min_watch_duration_ms: int
    min_trades_in_watch: int
    min_flow_imbalance: float
    min_event_flow_imbalance: float
    min_book_pressure: float
    min_resistance_withdrawal: float
    min_support_replenishment: float
    max_duration_ms: int


class ConfirmConfig(_Strict):
    min_flow_imbalance_short: float
    min_resistance_depletion: float
    min_microprice_rel: float
    min_book_pressure: float
    max_extension_sigma: float


class InvalidationConfig(_Strict):
    flow_reversal_imbalance: float
    book_pressure_reversal: float
    cvd_divergence_bps: float
    isolated_spike_share: float
    min_trades_short_window: int


class CooldownConfig(_Strict):
    after_confirm_ms: int
    after_reject_ms: int


class DetectorConfig(_Strict):
    gates: GateConfig
    watch: WatchConfig
    candidate: CandidateConfig
    confirm: ConfirmConfig
    invalidation: InvalidationConfig
    cooldown: CooldownConfig


class BookConfig(_Strict):
    require_contiguous_update_id: bool = True


class DemoConfig(_Strict):
    paths: PathsConfig = Field(default_factory=PathsConfig)
    datasets: tuple[DatasetConfig, ...]
    replay: ReplayConfig = Field(default_factory=ReplayConfig)
    book: BookConfig = Field(default_factory=BookConfig)
    features: FeatureConfig = Field(default_factory=FeatureConfig)
    detector: DetectorConfig

    def dataset(self, dataset_id: str) -> DatasetConfig:
        for d in self.datasets:
            if d.id == dataset_id:
                return d
        raise ConfigError(f"unknown dataset id {dataset_id!r}")

    def primary(self) -> DatasetConfig:
        primaries = [d for d in self.datasets if d.role == "primary"]
        if len(primaries) != 1:
            raise ConfigError("exactly one dataset must have role: primary")
        return primaries[0]


def load_config(path: Path) -> DemoConfig:
    """Load and validate a YAML config; raise :class:`ConfigError` on any problem."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    try:
        return DemoConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid config {path}:\n{exc}") from exc
