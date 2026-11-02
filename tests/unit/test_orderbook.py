from __future__ import annotations

import pytest

from bybit_demo.book.reconstruction import OrderBook
from bybit_demo.errors import BookNotInitializedError
from bybit_demo.models.orderbook import BookSide, ChangeType
from tests.helpers import D, delta, snap

BIDS = [("100.0", "1"), ("99.9", "2"), ("99.8", "3")]
ASKS = [("100.1", "1.5"), ("100.2", "2.5"), ("100.3", "3.5")]


@pytest.fixture
def book() -> OrderBook:
    b = OrderBook("TESTUSDT")
    b.apply_snapshot(snap(10, BIDS, ASKS))
    return b


def test_snapshot_initialization(book: OrderBook) -> None:
    assert book.is_initialized
    assert book.best_bid() == (D("100.0"), D("1"))
    assert book.best_ask() == (D("100.1"), D("1.5"))
    assert book.level_counts() == (3, 3)
    assert book.last_update_id == 10


def test_delta_before_snapshot_raises() -> None:
    with pytest.raises(BookNotInitializedError):
        OrderBook("X").apply_delta(delta(1, bids=[("1", "1")]))


def test_bid_update_insert_and_change(book: OrderBook) -> None:
    changes = book.apply_delta(delta(11, bids=[("99.9", "5"), ("100.05", "0.7")]))
    assert book.best_bid() == (D("100.05"), D("0.7"))
    assert book.quantity_at(BookSide.BID, D("99.9")) == D("5")
    kinds = {(c.price, c.change_type) for c in changes}
    assert kinds == {(D("99.9"), ChangeType.INCREASE), (D("100.05"), ChangeType.INSERT)}


def test_ask_update(book: OrderBook) -> None:
    book.apply_delta(delta(11, asks=[("100.1", "0.5")]))
    assert book.best_ask() == (D("100.1"), D("0.5"))


def test_zero_quantity_deletes_level(book: OrderBook) -> None:
    changes = book.apply_delta(delta(11, asks=[("100.1", "0")]))
    assert book.best_ask() == (D("100.2"), D("2.5"))
    assert changes[0].change_type is ChangeType.DELETE
    assert book.level_counts() == (3, 2)


def test_deleting_unknown_level_is_noop(book: OrderBook) -> None:
    assert book.apply_delta(delta(11, bids=[("50", "0")])) == []
    assert book.level_counts() == (3, 3)


def test_sorting_bids_descending_asks_ascending(book: OrderBook) -> None:
    book.apply_delta(delta(11, bids=[("99.95", "1")], asks=[("100.15", "1")]))
    bid_prices = [p for p, _ in book.bids()]
    ask_prices = [p for p, _ in book.asks()]
    assert bid_prices == sorted(bid_prices, reverse=True)
    assert ask_prices == sorted(ask_prices)
    assert bid_prices[1] == D("99.95") and ask_prices[1] == D("100.15")


def test_snapshot_replaces_book(book: OrderBook) -> None:
    book.apply_snapshot(snap(20, [("50", "1")], [("51", "1")]))
    assert book.level_counts() == (1, 1)
    assert book.best_bid() == (D("50"), D("1"))


def test_crossed_book_detected(book: OrderBook) -> None:
    assert not book.is_crossed()
    book.apply_delta(delta(11, bids=[("100.2", "1")]))
    assert book.is_crossed()


def test_top_spread_mid_microprice(book: OrderBook) -> None:
    top = book.top()
    assert top is not None
    assert top.mid == D("100.05")
    assert top.spread == D("0.1")
    # bid qty 1 < ask qty 1.5 -> microprice leans toward the bid
    assert top.microprice == (D("100.0") * D("1.5") + D("100.1") * D("1")) / D("2.5")
    assert top.microprice < top.mid


def test_depth_bands_single_pass_matches_individual(book: OrderBook) -> None:
    bands = (5.0, 15.0, 30.0)
    multi = book.depth_bands(bands)
    for b, m in zip(bands, multi, strict=True):
        single = book.depth_within_bps(b)
        assert (m.bid_qty, m.ask_qty, m.bid_notional, m.ask_notional) == (
            single.bid_qty,
            single.ask_qty,
            single.bid_notional,
            single.ask_notional,
        )
    # mid 100.05, 15 bps -> +/-0.150075: bids >= 99.899925 (100.0, 99.9), asks <= 100.200075
    assert multi[1].bid_qty == D("3") and multi[1].ask_qty == D("4")


def test_band_imbalance(book: OrderBook) -> None:
    band = book.depth_within_bps(30.0)
    expected = float(
        (band.bid_notional - band.ask_notional) / (band.bid_notional + band.ask_notional)
    )
    assert band.imbalance == pytest.approx(expected)


def test_compare_to_snapshot(book: OrderBook) -> None:
    assert book.compare_to_snapshot(snap(10, BIDS, ASKS)).matches
    diff = book.compare_to_snapshot(snap(10, [("100.0", "9"), *BIDS[1:]], ASKS))
    assert not diff.matches and diff.bid_mismatches == 1 and diff.ask_mismatches == 0


def test_invalidate_keeps_levels_but_blocks_deltas(book: OrderBook) -> None:
    book.invalidate()
    assert not book.is_initialized
    assert book.top() is not None
    with pytest.raises(BookNotInitializedError):
        book.apply_delta(delta(11, bids=[("1", "1")]))
