from __future__ import annotations

import gzip
import json
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from bybit_demo.config import DatasetConfig, ProviderId, load_config
from bybit_demo.data.downloader import ManifestStore, download_all, sha256_file
from bybit_demo.data.normalization import (
    iter_bybit_orderbook,
    iter_tardis_book,
    load_bybit_trades,
    load_tardis_trades,
)
from bybit_demo.data.provider_registry import (
    FileKind,
    bybit_orderbook_url,
    bybit_trades_url,
    dataset_files,
    tardis_url,
)
from bybit_demo.data.validators import ValidationReport, parse_decimal
from bybit_demo.errors import ConfigError, DownloadError, SchemaError
from bybit_demo.models.events import BookDelta, BookSnapshot, Side

GOOD_SNAP: dict[str, Any] = {
    "topic": "orderbook.200.TESTUSDT",
    "type": "snapshot",
    "ts": 1000,
    "cts": 990,
    "data": {"s": "TESTUSDT", "b": [["10.0", "1"]], "a": [["10.1", "2"]], "u": 5, "seq": 50},
}
GOOD_DELTA: dict[str, Any] = {
    "topic": "orderbook.200.TESTUSDT",
    "type": "delta",
    "ts": 1100,
    "cts": 1095,
    "data": {"s": "TESTUSDT", "b": [["10.0", "0"]], "a": [], "u": 6, "seq": 51},
}


def write_zip(path: Path, lines: list[str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("x.data", "\n".join(lines) + "\n")
    return path


def test_bybit_orderbook_parse_and_clock(tmp_path: Path) -> None:
    p = write_zip(tmp_path / "ob.zip", [json.dumps(GOOD_SNAP), json.dumps(GOOD_DELTA)])
    rep = ValidationReport("ob")
    evs = list(iter_bybit_orderbook(p, rep, book_clock="cts"))
    assert isinstance(evs[0], BookSnapshot) and isinstance(evs[1], BookDelta)
    assert evs[0].exchange_ts_us == 990_000 and evs[0].system_ts_us == 1_000_000
    assert evs[0].update_id == 5 and evs[0].cross_seq == 50 and evs[0].depth == 200
    assert evs[1].bid_updates[0].quantity == 0
    ts_clock = list(iter_bybit_orderbook(p, ValidationReport("ob"), book_clock="ts"))
    assert ts_clock[0].exchange_ts_us == 1_000_000
    assert rep.records_ok == 2 and rep.records_malformed == 0


@pytest.mark.parametrize(
    "bad",
    [
        "{not json",
        json.dumps({**GOOD_DELTA, "type": "weird"}),
        json.dumps({k: v for k, v in GOOD_DELTA.items() if k != "ts"}),
        json.dumps({**GOOD_DELTA, "data": {**GOOD_DELTA["data"], "u": "6"}}),
        json.dumps({**GOOD_DELTA, "data": {**GOOD_DELTA["data"], "b": [["10.0"]]}}),
        json.dumps({**GOOD_DELTA, "data": {**GOOD_DELTA["data"], "b": [["abc", "1"]]}}),
        json.dumps({**GOOD_DELTA, "data": {**GOOD_DELTA["data"], "b": [["10", "-1"]]}}),
        json.dumps({**GOOD_SNAP, "data": {**GOOD_SNAP["data"], "b": [["10", "0"]]}}),
    ],
)
def test_malformed_orderbook_rows_counted_in_lenient_mode(tmp_path: Path, bad: str) -> None:
    p = write_zip(tmp_path / "ob.zip", [json.dumps(GOOD_SNAP), bad, json.dumps(GOOD_DELTA)])
    rep = ValidationReport("ob")
    evs = list(iter_bybit_orderbook(p, rep))
    assert len(evs) == 2
    assert rep.records_malformed == 1
    assert ":2:" in rep.examples[0] or "ob.zip:2" in rep.examples[0]


def test_strict_mode_raises_on_first_malformed_row(tmp_path: Path) -> None:
    p = write_zip(tmp_path / "ob.zip", [json.dumps(GOOD_SNAP), "{bad"])
    with pytest.raises(SchemaError):
        list(iter_bybit_orderbook(p, ValidationReport("ob", strict=True)))


def test_unexpected_symbol_rejected(tmp_path: Path) -> None:
    p = write_zip(tmp_path / "ob.zip", [json.dumps(GOOD_SNAP)])
    rep = ValidationReport("ob")
    assert list(iter_bybit_orderbook(p, rep, symbol="OTHER")) == []
    assert rep.records_malformed == 1


def write_gz(path: Path, text: str) -> Path:
    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        f.write(text)
    return path


TRADE_HEADER = "timestamp,symbol,side,size,price,tickDirection,trdMatchID,RPI\n"


def test_bybit_trades_parse_and_missing_fields(tmp_path: Path) -> None:
    body = TRADE_HEADER + (
        "100.1234,TESTUSDT,Buy,2,10.5,PlusTick,id1,0\n"
        "100.2,TESTUSDT,Sell,,10.4,MinusTick,id2,0\n"  # missing size
        "100.3,TESTUSDT,Hold,1,10.4,MinusTick,id3,0\n"  # bad side
        "100.4,TESTUSDT,Sell,1,10.4,MinusTick,id1,0\n"  # duplicate id
        "100.5,TESTUSDT,Sell,1,10.4,MinusTick,id4,1\n"  # RPI trade
    )
    rep = ValidationReport("t")
    trades, info = load_bybit_trades(write_gz(tmp_path / "t.csv.gz", body), rep)
    assert [t.trade_id for t in trades] == ["id1", "id4"]
    assert trades[0].exchange_ts_us == 100_123_400 and trades[0].side is Side.BUY
    assert rep.records_malformed == 3 and info.duplicate_ids == 1 and info.rpi_trades == 1


def test_bybit_trades_missing_column_is_fatal(tmp_path: Path) -> None:
    with pytest.raises(SchemaError, match="missing CSV columns"):
        load_bybit_trades(
            write_gz(tmp_path / "t.csv.gz", "timestamp,symbol,side\n1,X,Buy\n"),
            ValidationReport("t"),
        )


def test_out_of_order_trade_file_is_stably_sorted_and_reported(tmp_path: Path) -> None:
    body = TRADE_HEADER + "5,T,Buy,1,1,x,a,0\n3,T,Buy,1,1,x,b,0\n3,T,Sell,1,1,x,c,0\n"
    trades, info = load_bybit_trades(write_gz(tmp_path / "t.csv.gz", body), ValidationReport("t"))
    assert [t.trade_id for t in trades] == ["b", "c", "a"]
    assert info.reordered and info.out_of_order_rows == 1


def test_tardis_book_rows_grouped_into_events(tmp_path: Path) -> None:
    body = (
        "exchange,symbol,timestamp,local_timestamp,is_snapshot,side,price,amount\n"
        "bybit,T,100,105,true,ask,10.1,1\n"
        "bybit,T,100,105,true,bid,10.0,2\n"
        "bybit,T,200,205,false,bid,10.0,0\n"
        "bybit,T,200,205,false,ask,10.2,3\n"
        "bybit,T,300,305,false,ask,bad,3\n"
        "bybit,T,400,405,false,ask,10.1,0\n"
    )
    rep = ValidationReport("tb")
    evs = list(iter_tardis_book(write_gz(tmp_path / "b.csv.gz", body), rep))
    assert [type(e).__name__ for e in evs] == ["BookSnapshot", "BookDelta", "BookDelta"]
    assert len(evs[0].bids) == 1 and len(evs[0].asks) == 1  # type: ignore[union-attr]
    assert evs[1].exchange_ts_us == 200 and evs[1].local_ts_us == 205
    assert evs[0].update_id is None
    assert rep.records_malformed == 1


def test_tardis_trades(tmp_path: Path) -> None:
    body = (
        "exchange,symbol,timestamp,local_timestamp,id,side,price,amount\n"
        "bybit,T,100,110,x1,buy,10,1\nbybit,T,200,210,x2,sell,10,2\nbybit,T,300,310,x3,??,10,2\n"
    )
    rep = ValidationReport("tt")
    trades, _ = load_tardis_trades(write_gz(tmp_path / "t.csv.gz", body), rep)
    assert [t.side for t in trades] == [Side.BUY, Side.SELL]
    assert rep.records_malformed == 1


def test_parse_decimal_rules() -> None:
    assert parse_decimal("1.50", "x", source="s", line=1, positive=True) == parse_decimal(
        "1.5", "x", source="s", line=1, positive=True
    )
    for bad, pos in (("0", True), ("-1", False), ("NaN", False), ("inf", False), ("x", False)):
        with pytest.raises(SchemaError):
            parse_decimal(bad, "x", source="s", line=1, positive=pos)
    with pytest.raises(SchemaError):
        parse_decimal(1.5, "x", source="s", line=1, positive=True)


def test_provider_urls_match_verified_patterns() -> None:
    d = date(2026, 10, 1)
    assert bybit_orderbook_url("ORDIUSDT", d, 200) == (
        "https://quote-saver.bycsi.com/orderbook/linear/ORDIUSDT/2026-10-01_ORDIUSDT_ob200.data.zip"
    )
    assert bybit_trades_url("ORDIUSDT", d) == (
        "https://public.bybit.com/trading/ORDIUSDT/ORDIUSDT2026-10-01.csv.gz"
    )
    assert tardis_url("trades", "ORDIUSDT", d) == (
        "https://datasets.tardis.dev/v1/bybit/trades/2026/10/01/ORDIUSDT.csv.gz"
    )


def test_dataset_files_validation(tmp_path: Path) -> None:
    ds = DatasetConfig(id="x", provider=ProviderId.TARDIS_FREE, symbol="X", date=date(2026, 10, 2))
    with pytest.raises(ConfigError, match="1st of each month"):
        dataset_files(ds, tmp_path)
    ds2 = DatasetConfig(
        id="y", provider=ProviderId.BYBIT_ARCHIVE, symbol="X", date=date(2026, 10, 2)
    )
    with pytest.raises(ConfigError, match="depth"):
        dataset_files(ds2, tmp_path)
    ok = dataset_files(ds2.model_copy(update={"depth": 200}), tmp_path)
    assert [f.kind for f in ok] == [FileKind.BOOK, FileKind.TRADES]


def test_config_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("datasets: []\nunknown_key: 1\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(bad)


async def test_downloader_records_hash_and_uses_cache(tmp_path: Path) -> None:
    zpath = write_zip(tmp_path / "payload.zip", [json.dumps(GOOD_SNAP)])
    payloads = {".zip": zpath.read_bytes(), ".gz": gzip.compress(b"timestamp\n1\n")}
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        if "missing" in str(req.url):
            return httpx.Response(404, text="nope")
        body = payloads[".zip" if str(req.url).endswith(".zip") else ".gz"]
        return httpx.Response(200, content=body, headers={"last-modified": "Thu, 01 Jan 2026"})

    ds = DatasetConfig(
        id="d", provider=ProviderId.BYBIT_ARCHIVE, symbol="X", date=date(2026, 1, 1), depth=200
    )
    files = dataset_files(ds, tmp_path / "raw")
    manifest = ManifestStore(tmp_path / "manifest.json")
    recs = await download_all(files, manifest, transport=httpx.MockTransport(handler))
    assert [r.bytes for r in recs] == [len(payloads[".zip"]), len(payloads[".gz"])]
    assert not any(r.from_cache for r in recs)
    assert recs[0].sha256 == sha256_file(files[0].local_path)
    saved = {r["url"]: r["sha256"] for r in json.loads((tmp_path / "manifest.json").read_text())}
    assert saved[files[0].url] == recs[0].sha256
    again = await download_all(
        files, ManifestStore(tmp_path / "manifest.json"), transport=httpx.MockTransport(handler)
    )
    assert all(r.from_cache for r in again) and len(calls) == 2

    bad = [files[0].__class__(ds.id, FileKind.BOOK, "https://x/missing", tmp_path / "m.zip")]
    with pytest.raises(DownloadError, match="404"):
        await download_all(bad, manifest, transport=httpx.MockTransport(handler))
    assert not (tmp_path / "m.zip").exists() and not (tmp_path / "m.zip.part").exists()
