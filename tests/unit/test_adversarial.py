"""Adversarial tests: corrupted inputs, network failures, ordering and leakage attacks.

Each test states the invariant it protects. None of them uses market data.
"""

from __future__ import annotations

import gzip
import io
import json
import zipfile
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from bybit_demo.audit.evidence import write_json
from bybit_demo.book.continuity import BookSynchronizer, BybitContinuityValidator
from bybit_demo.book.reconstruction import OrderBook
from bybit_demo.config import DatasetConfig, ProviderId, load_config
from bybit_demo.data.downloader import (
    ManifestStore,
    check_archive_integrity,
    download_all,
    sha256_file,
)
from bybit_demo.data.normalization import iter_bybit_orderbook, load_bybit_trades
from bybit_demo.data.provider_registry import FileKind, RemoteFile, dataset_files
from bybit_demo.data.validators import ValidationReport
from bybit_demo.data.verification import AggressorCheck, _verdict
from bybit_demo.detector.rules import DirectionalView, confirm_rules
from bybit_demo.errors import CorruptFileError, DownloadError, IntegrityError, SchemaError
from bybit_demo.features.feature_engine import FeatureEngine
from bybit_demo.flow.cvd import CVDTracker, FlowWindow
from bybit_demo.models.events import MarketEvent, Side, TradeEvent, iso_to_us, us_to_iso
from bybit_demo.models.signals import Direction
from bybit_demo.pipeline import run_replay
from tests.fixtures.synthetic import scenarios
from tests.helpers import CONFIG_PATH, D, delta, demo_config, make_fv, run_events, snap, trade

OB_SNAP: dict[str, Any] = {
    "topic": "orderbook.200.TESTUSDT",
    "type": "snapshot",
    "ts": 1000,
    "cts": 990,
    "data": {"s": "TESTUSDT", "b": [["10.0", "1"]], "a": [["10.1", "2"]], "u": 5, "seq": 50},
}


def _zip(path: Path, lines: list[str]) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("x.data", "\n".join(lines) + "\n")
    return path


def _many_lines(n: int = 2_000) -> list[str]:
    out = [json.dumps(OB_SNAP)]
    for i in range(n):
        d = {**OB_SNAP, "type": "delta", "ts": 1100 + i, "cts": 1095 + i}
        d["data"] = {**OB_SNAP["data"], "b": [["10.0", str(i + 2)]], "a": [], "u": 6 + i}
        out.append(json.dumps(d))
    return out


# ------------------------------------------------------------ corrupt files
def test_truncated_zip_archive_is_rejected_not_read_partially(tmp_path: Path) -> None:
    """A cut-off download must never look like a shorter, valid day."""
    good = _zip(tmp_path / "ob.data.zip", _many_lines())
    data = good.read_bytes()
    bad = tmp_path / "cut.data.zip"
    bad.write_bytes(data[: len(data) // 2])
    with pytest.raises(CorruptFileError, match="truncated or corrupt"):
        list(iter_bybit_orderbook(bad, ValidationReport("ob")))
    with pytest.raises(CorruptFileError):
        check_archive_integrity(bad)


def test_bit_flipped_zip_member_fails_crc(tmp_path: Path) -> None:
    good = _zip(tmp_path / "ob.data.zip", _many_lines())
    raw = bytearray(good.read_bytes())
    raw[200] ^= 0xFF  # inside the compressed member
    bad = tmp_path / "flip.data.zip"
    bad.write_bytes(bytes(raw))
    with pytest.raises(CorruptFileError):
        check_archive_integrity(bad)
    with pytest.raises(CorruptFileError):
        list(iter_bybit_orderbook(bad, ValidationReport("ob")))


def test_truncated_gzip_trade_file_is_rejected(tmp_path: Path) -> None:
    body = "timestamp,symbol,side,size,price\n" + "".join(
        f"{100 + i}.5,TESTUSDT,Buy,1,10\n" for i in range(5_000)
    )
    p = tmp_path / "t.csv.gz"
    with gzip.open(p, "wt", encoding="utf-8") as f:
        f.write(body)
    data = p.read_bytes()
    cut = tmp_path / "cut.csv.gz"
    cut.write_bytes(data[: len(data) - 40])
    with pytest.raises(CorruptFileError):
        load_bybit_trades(cut, ValidationReport("t"))
    with pytest.raises(CorruptFileError):
        check_archive_integrity(cut)
    notgz = tmp_path / "plain.csv.gz"
    notgz.write_text(body, encoding="utf-8")
    with pytest.raises(CorruptFileError):
        load_bybit_trades(notgz, ValidationReport("t"))


def test_truncated_final_json_line_aborts_strict_replay(tmp_path: Path) -> None:
    lines = _many_lines(10)
    lines[-1] = lines[-1][: len(lines[-1]) // 2]
    p = _zip(tmp_path / "ob.data.zip", lines)
    with pytest.raises(SchemaError, match="invalid JSON"):
        list(iter_bybit_orderbook(p, ValidationReport("ob", strict=True)))


def test_duplicate_price_in_one_message_is_rejected(tmp_path: Path) -> None:
    """Two quantities for one price in one message have no defined order: refuse it."""
    msg = {**OB_SNAP, "data": {**OB_SNAP["data"], "b": [["10.0", "1"], ["10.00", "3"]]}}
    p = _zip(tmp_path / "ob.data.zip", [json.dumps(msg)])
    with pytest.raises(SchemaError, match="twice"):
        list(iter_bybit_orderbook(p, ValidationReport("ob", strict=True)))


# --------------------------------------------------------- downloader attacks
def _files(tmp_path: Path) -> list[RemoteFile]:
    ds = DatasetConfig(
        id="d", provider=ProviderId.BYBIT_ARCHIVE, symbol="X", date=date(2026, 1, 1), depth=200
    )
    return dataset_files(ds, tmp_path / "raw")


def _zip_bytes(msg: dict[str, Any]) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as zf:
        zf.writestr("x.data", json.dumps(msg) + "\n")
    return b.getvalue()


def _payloads() -> dict[str, bytes]:
    g = gzip.compress(b"timestamp,symbol,side,size,price\n1.0,X,Buy,1,1\n")
    return {"zip": _zip_bytes(OB_SNAP), "gz": g}


def _handler(payloads: dict[str, bytes], calls: list[str]) -> Any:
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        body = payloads["zip"] if str(req.url).endswith(".zip") else payloads["gz"]
        return httpx.Response(200, content=body)

    return handler


async def test_corrupted_cache_is_redownloaded_not_trusted(tmp_path: Path) -> None:
    files, pl = _files(tmp_path), _payloads()
    calls: list[str] = []
    manifest_path = tmp_path / "m.json"
    t = httpx.MockTransport(_handler(pl, calls))
    await download_all(files, ManifestStore(manifest_path), transport=t)
    files[0].local_path.write_bytes(b"garbage")  # cache tampered with
    recs = await download_all(files, ManifestStore(manifest_path), transport=t)
    assert not recs[0].from_cache and recs[1].from_cache
    assert files[0].local_path.read_bytes() == pl["zip"]
    assert len(calls) == 3


async def test_unrecorded_corrupt_cache_is_redownloaded(tmp_path: Path) -> None:
    files, pl = _files(tmp_path), _payloads()
    calls: list[str] = []
    for f in files:
        f.local_path.parent.mkdir(parents=True, exist_ok=True)
        f.local_path.write_bytes(b"not an archive")
    t = httpx.MockTransport(_handler(pl, calls))
    recs = await download_all(files, ManifestStore(tmp_path / "m.json"), transport=t)
    assert [r.from_cache for r in recs] == [False, False] and len(calls) == 2


async def test_provider_file_changed_raises_integrity_error(tmp_path: Path) -> None:
    """Evidence recorded for one file must never silently describe a different file."""
    files, pl = _files(tmp_path), _payloads()
    manifest_path = tmp_path / "m.json"
    await download_all(
        files, ManifestStore(manifest_path), transport=httpx.MockTransport(_handler(pl, []))
    )
    original = sha256_file(files[0].local_path)
    files[0].local_path.unlink()
    changed = {**pl, "zip": _zip_bytes({**OB_SNAP, "ts": 2})}
    with pytest.raises(IntegrityError, match="differs from the manifest"):
        await download_all(
            files,
            ManifestStore(manifest_path),
            transport=httpx.MockTransport(_handler(changed, [])),
        )
    assert not files[0].local_path.exists()  # the changed file is not installed
    rec = ManifestStore(manifest_path).get(files[0].url)
    assert rec is not None and rec.sha256 == original


async def test_transient_5xx_is_retried_then_succeeds(tmp_path: Path) -> None:
    files, pl = _files(tmp_path)[:1], _payloads()
    n = {"calls": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        n["calls"] += 1
        if n["calls"] < 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, content=pl["zip"])

    recs = await download_all(
        files,
        ManifestStore(tmp_path / "m.json"),
        transport=httpx.MockTransport(handler),
        backoff_s=0.0,
    )
    assert n["calls"] == 3 and recs[0].sha256 == sha256_file(files[0].local_path)


async def test_404_is_not_retried(tmp_path: Path) -> None:
    files = _files(tmp_path)[:1]
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return httpx.Response(404, text="NoSuchKey")

    with pytest.raises(DownloadError, match="404"):
        await download_all(
            files,
            ManifestStore(tmp_path / "m.json"),
            transport=httpx.MockTransport(handler),
            backoff_s=0.0,
        )
    assert len(calls) == 1 and not files[0].local_path.exists()


async def test_network_down_without_cache_fails_explicitly(tmp_path: Path) -> None:
    files = _files(tmp_path)[:1]

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=req)

    with pytest.raises(DownloadError, match="unavailable offline"):
        await download_all(
            files,
            ManifestStore(tmp_path / "m.json"),
            transport=httpx.MockTransport(handler),
            backoff_s=0.0,
        )
    part = files[0].local_path.with_suffix(files[0].local_path.suffix + ".part")
    assert not files[0].local_path.exists() and not part.exists()


async def test_short_body_vs_content_length_is_rejected(tmp_path: Path) -> None:
    files, pl = _files(tmp_path)[:1], _payloads()

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=pl["zip"], headers={"content-length": str(len(pl["zip"]) + 100)}
        )

    with pytest.raises(DownloadError, match="Content-Length"):
        await download_all(
            files,
            ManifestStore(tmp_path / "m.json"),
            transport=httpx.MockTransport(handler),
            backoff_s=0.0,
        )
    assert not files[0].local_path.exists()


async def test_corrupt_download_is_never_installed(tmp_path: Path) -> None:
    files = _files(tmp_path)[:1]

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"PK\x03\x04 broken")

    with pytest.raises(CorruptFileError):
        await download_all(
            files,
            ManifestStore(tmp_path / "m.json"),
            transport=httpx.MockTransport(handler),
            backoff_s=0.0,
        )
    assert not files[0].local_path.exists()


# --------------------------------------------------------------- book state
def _sync() -> BookSynchronizer:
    return BookSynchronizer(OrderBook("TESTUSDT"), BybitContinuityValidator("TESTUSDT"))


def test_locked_book_counts_as_crossed_and_blocks_use() -> None:
    s = _sync()
    s.process(snap(1, [("10", "1")], [("11", "1")]))
    upd = s.process(delta(2, bids=[("11", "1")]))  # bid == ask: locked
    assert [i.kind.value for i in upd.issues] == ["CROSSED_BOOK"] and not s.usable
    s.process(delta(3, bids=[("11", "0")]))
    assert s.usable


def test_empty_side_produces_no_feature_vector() -> None:
    cfg = demo_config()
    fe = FeatureEngine(cfg.features, cfg.detector.gates)
    s = _sync()
    upd = s.process(snap(1, [("10", "1")], []))
    fe.on_book(1_000, upd, s.book, s.usable)
    assert fe.compute(1_000, 0, s.book, synced=True, crossed=False) is None


def test_gap_then_resync_restores_exact_snapshot_state() -> None:
    s = _sync()
    s.process(snap(1, [("10", "1")], [("11", "1")]))
    s.process(delta(3, bids=[("10", "5")]))  # gap: u 2 missing
    assert not s.usable
    assert not s.process(delta(4, bids=[("9", "1")])).applied
    s.process(snap(10, [("10", "2")], [("12", "1")]))
    assert s.usable and list(s.book.bids()) == [(D(10), D(2))]
    assert list(s.book.asks()) == [(D(12), D(1))]


# -------------------------------------------------------- aggressor / CVD
def test_aggressor_check_hand_cases() -> None:
    s = _sync()
    s.process(snap(1, [("10", "1")], [("11", "1")]))
    chk = AggressorCheck()
    for side, px in ((Side.BUY, "11"), (Side.SELL, "10"), (Side.BUY, "10"), (Side.SELL, "10.5")):
        chk.observe(trade(1, side, px, "1"), s)
    d = chk.to_dict()
    assert d["consistent_with_taker_side"] == 2 and d["consistent_with_maker_side"] == 1
    assert d["counts"]["sell_inside_spread"] == 1


def test_cvd_and_imbalance_hand_calculated() -> None:
    """buy 2@10, sell 1@11, buy 0.5@12 -> buy 26, sell 11 USDT."""
    trades = [
        trade(1, Side.BUY, "10", "2"),
        trade(2, Side.SELL, "11", "1"),
        trade(3, Side.BUY, "12", "0.5"),
    ]
    cvd, w = CVDTracker(), FlowWindow(5_000_000)
    for t in trades:
        cvd.on_trade(t)
        w.on_trade(t)
    assert cvd.cvd_notional == Decimal("15") and cvd.cvd_qty == Decimal("1.5")
    assert w.signed_notional == Decimal("15") and w.total_notional == Decimal("37")
    assert w.imbalance == pytest.approx(15 / 37)
    assert w.count_imbalance == pytest.approx(1 / 3)
    w.advance(5_000_001)  # trade at t=1 leaves the (now - 5 s, now] window
    assert w.signed_notional == Decimal("-5") and w.trade_count == 2


# ------------------------------------------------------- ordering / leakage
def test_replay_output_independent_of_stream_argument_order() -> None:
    sc = scenarios.long_breakout()
    s1, k1 = run_events([sc.book, sc.trades])
    s2, k2 = run_events([sc.trades, sc.book])
    assert [r.to_dict() for r in k1.transitions] == [r.to_dict() for r in k2.transitions]
    assert s1.deterministic_dict() == s2.deterministic_dict()


def _perturb_after(events: list[MarketEvent], cut_us: int) -> list[MarketEvent]:
    out: list[MarketEvent] = []
    for ev in events:
        if isinstance(ev, TradeEvent) and ev.exchange_ts_us > cut_us:
            out.append(replace(ev, side=Side.SELL, quantity=ev.quantity * 1000))
        else:
            out.append(ev)
    return out


@pytest.mark.parametrize("factory", scenarios.ALL)
def test_changing_the_future_never_changes_the_past(factory: Any) -> None:
    """Stronger than prefix truncation: future events exist but are adversarially altered."""
    sc = factory()
    _, base = run_events([sc.book, sc.trades])
    assert base.transitions, "scenario must produce transitions"
    cut = base.transitions[len(base.transitions) // 2].ts_us
    _, alt = run_events([sc.book, _perturb_after(list(sc.trades), cut)])
    before = [r.to_dict() for r in base.transitions if r.ts_us <= cut]
    assert before == [r.to_dict() for r in alt.transitions if r.ts_us <= cut]


def test_long_and_short_cannot_confirm_on_the_same_vector() -> None:
    cfg = demo_config().detector.confirm
    for imb in (-1.0, -0.3, -0.1, 0.0, 0.1, 0.3, 1.0):
        for cvd in (-100.0, 0.0, 100.0):
            fv = make_fv(
                imb_short=imb,
                cvd_short_usdt=cvd,
                microprice_rel=0.0,
                book_pressure=0.0,
                ask_depletion=1.0,
                bid_depletion=1.0,
            )
            long_ok = confirm_rules(DirectionalView(fv, Direction.LONG), cfg).ok
            short_ok = confirm_rules(DirectionalView(fv, Direction.SHORT), cfg).ok
            assert not (long_ok and short_ok)


# --------------------------------------------------------------- timestamps
def test_timestamp_rendering_is_exact_to_the_microsecond() -> None:
    for us in (1_790_812_802_118_700, 1_790_812_802_118_701, 1_790_812_799_999_999, 0):
        assert iso_to_us(us_to_iso(us)) == us
    with pytest.raises(ValueError, match="UTC offset"):
        iso_to_us("2026-10-01T00:00:00")


def _verdict_input(first: int, last: int, book_first: int, book_last: int) -> dict[str, Any]:
    book = {
        "snapshots": 2,
        "deltas": 1,
        "continuity_failures": 0,
        "continuity_issue_counts": {},
        "checkpoint_matches": 1,
        "checkpoint_comparisons": 1,
        "first_ts_us": book_first,
        "last_ts_us": book_last,
        "max_bid_levels": 200,
        "max_ask_levels": 200,
        "update_id_not_plus_one_deltas": 0,
    }
    return {
        "provider": "bybit_archive",
        "book": book,
        "book_schema_validation": {"records_malformed": 0},
        "trades_schema_validation": {"records_malformed": 0},
        "trades": {"rows": 10, "first_ts_us": first, "last_ts_us": last},
        "aggressor_side_vs_book": {
            "taker_consistent_share": 0.9,
            "consistent_with_taker_side": 9,
            "consistent_with_maker_side": 1,
        },
    }


def test_wrong_date_file_fails_verification() -> None:
    ds = DatasetConfig(
        id="d", provider=ProviderId.BYBIT_ARCHIVE, symbol="X", date=date(2026, 10, 1), depth=200
    )
    day = iso_to_us("2026-10-01T00:00:00Z")
    ok = _verdict(_verdict_input(day + 1, day + 10, day - 83_000, day + 86_400_211_000), ds)
    assert ok["pass"]
    nxt = 86_400_000_000
    wrong = _verdict(_verdict_input(day + nxt + 1, day + nxt + 10, day, day + 10), ds)
    assert not wrong["checks"]["trade_timestamps_inside_utc_day"] and not wrong["pass"]
    bad_depth = _verdict_input(day + 1, day + 2, day, day + 2)
    bad_depth["book"]["max_bid_levels"] = 201
    assert not _verdict(bad_depth, ds)["checks"]["book_never_exceeds_advertised_depth"]


def test_replay_pipeline_aborts_on_malformed_record(tmp_path: Path) -> None:
    """The replay never skips a corrupt line (verify-data counts them; replay refuses)."""
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    raw["paths"] = {
        "raw_dir": str(tmp_path / "raw"),
        "evidence_dir": str(tmp_path / "ev"),
        "synthetic_output_dir": str(tmp_path / "s"),
    }
    raw["datasets"] = [
        {
            "id": "SYNTHETIC_x",
            "provider": "bybit_archive",
            "symbol": scenarios.SYMBOL,
            "date": "2000-01-01",
            "depth": 200,
            "role": "primary",
            "note": scenarios.MARKER,
        }
    ]
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    cfg = load_config(cfg_path)
    book, trades = scenarios.write_bybit_format(scenarios.long_breakout(), tmp_path / "gen")
    targets = {f.kind: f.local_path for f in dataset_files(cfg.primary(), cfg.paths.raw_dir)}
    lines = zipfile.ZipFile(book).read(zipfile.ZipFile(book).namelist()[0]).decode().splitlines()
    lines.insert(3, "{corrupt")
    targets[FileKind.BOOK].parent.mkdir(parents=True, exist_ok=True)
    _zip(targets[FileKind.BOOK], lines)
    targets[FileKind.TRADES].write_bytes(trades.read_bytes())
    with pytest.raises(SchemaError, match="invalid JSON"):
        run_replay(cfg, cfg.primary(), tmp_path / "out")


def test_evidence_files_use_lf_on_every_platform(tmp_path: Path) -> None:
    """CRLF on Windows would change every evidence SHA-256 versus a Linux checkout."""
    write_json(tmp_path / "e.json", {"b": [1, 2], "a": "x"})
    m = ManifestStore(tmp_path / "m.json")
    m.save()
    for name in ("e.json", "m.json"):
        assert b"\r" not in (tmp_path / name).read_bytes()
