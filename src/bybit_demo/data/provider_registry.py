"""Known historical-data sources and their URL conventions.

URL patterns below were confirmed by actually requesting them (see
docs/DATA_FEASIBILITY_REPORT.md); nothing here is assumed from marketing text.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path

from bybit_demo.config import DatasetConfig, ProviderId
from bybit_demo.errors import ConfigError


class FileKind(StrEnum):
    BOOK = "book"
    TRADES = "trades"


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    id: ProviderId
    name: str
    official: bool
    landing_url: str
    docs_url: str
    access: str


PROVIDERS: dict[ProviderId, ProviderSpec] = {
    ProviderId.BYBIT_ARCHIVE: ProviderSpec(
        id=ProviderId.BYBIT_ARCHIVE,
        name="Bybit official historical market data",
        official=True,
        landing_url="https://www.bybit.com/derivatives/en/history-data",
        docs_url="https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook",
        access="Public HTTPS, no account or API key.",
    ),
    ProviderId.TARDIS_FREE: ProviderSpec(
        id=ProviderId.TARDIS_FREE,
        name="Tardis.dev (free first-day-of-month CSV samples)",
        official=False,
        landing_url="https://tardis.dev",
        docs_url="https://docs.tardis.dev/historical-data-details/bybit",
        access="First day of each month free without API key; other days require a paid key.",
    ),
}


@dataclass(frozen=True, slots=True)
class RemoteFile:
    dataset_id: str
    kind: FileKind
    url: str
    local_path: Path


def bybit_orderbook_url(symbol: str, day: date, depth: int) -> str:
    """Official orderbook archive (snapshot + delta JSON lines in a .zip)."""
    d = day.isoformat()
    return (
        f"https://quote-saver.bycsi.com/orderbook/linear/{symbol}/{d}_{symbol}_ob{depth}.data.zip"
    )


def bybit_trades_url(symbol: str, day: date) -> str:
    """Official public trade archive (one gzip CSV per symbol per UTC day)."""
    return f"https://public.bybit.com/trading/{symbol}/{symbol}{day.isoformat()}.csv.gz"


def tardis_url(data_type: str, symbol: str, day: date) -> str:
    return (
        f"https://datasets.tardis.dev/v1/bybit/{data_type}/"
        f"{day.year:04d}/{day.month:02d}/{day.day:02d}/{symbol}.csv.gz"
    )


def dataset_files(ds: DatasetConfig, raw_dir: Path) -> list[RemoteFile]:
    """Remote files making up one configured dataset, with deterministic local paths."""
    base = raw_dir / ds.provider.value / ds.symbol
    day = ds.date.isoformat()
    if ds.provider is ProviderId.BYBIT_ARCHIVE:
        if ds.depth is None:
            raise ConfigError(f"{ds.id}: bybit_archive datasets need 'depth' (200 or 500)")
        book_url = bybit_orderbook_url(ds.symbol, ds.date, ds.depth)
        trades_url = bybit_trades_url(ds.symbol, ds.date)
        book_name = f"{day}_{ds.symbol}_ob{ds.depth}.data.zip"
        trades_name = f"{ds.symbol}{day}.csv.gz"
    elif ds.provider is ProviderId.TARDIS_FREE:
        if ds.date.day != 1:
            raise ConfigError(
                f"{ds.id}: Tardis.dev serves only the 1st of each month without an API key"
            )
        book_url = tardis_url("incremental_book_L2", ds.symbol, ds.date)
        trades_url = tardis_url("trades", ds.symbol, ds.date)
        book_name = f"{day}_{ds.symbol}_incremental_book_L2.csv.gz"
        trades_name = f"{day}_{ds.symbol}_trades.csv.gz"
    else:  # pragma: no cover - enum is exhaustive
        raise ConfigError(f"unsupported provider {ds.provider}")
    return [
        RemoteFile(ds.id, FileKind.BOOK, book_url, base / book_name),
        RemoteFile(ds.id, FileKind.TRADES, trades_url, base / trades_name),
    ]
