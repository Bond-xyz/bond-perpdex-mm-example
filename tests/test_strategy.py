import copy
from decimal import Decimal

import pytest

from bond_perpdex import SafetyError
from bond_perpdex.offline import DEPTH_FRAME, NOW_MS, SNAPSHOT
from bond_perpdex.strategy import DepthBook, MakerPolicy


def synced_book():
    book = DepthBook("BTCUSDCPERP")
    book.snapshot(SNAPSHOT)
    book.apply(DEPTH_FRAME, NOW_MS)
    return book


def test_strategy_inventory_worst_case_and_rounding(connected):
    market = connected[3]
    policy = MakerPolicy()
    for position, sides in [("0", ["BUY", "SELL"]), ("0.010", ["SELL"]), ("-0.010", ["BUY"])]:
        quotes = policy.quotes(
            market,
            synced_book(),
            position=Decimal(position),
            position_observed_ms=NOW_MS,
            now_ms=NOW_MS,
            outstanding=[],
        )
        assert [quote.side for quote in quotes] == sides
        for quote in quotes:
            assert quote.price % market.tick == 0
            assert quote.price * quote.quantity <= policy.max_order_notional
            assert (
                quote.price < Decimal("65010")
                if quote.side == "BUY"
                else quote.price > Decimal("65000")
            )


def test_snapshot_alone_disconnect_and_staleness_never_quote():
    book = DepthBook("BTCUSDCPERP")
    book.snapshot(SNAPSHOT)
    with pytest.raises(SafetyError):
        book.top(NOW_MS)
    book = synced_book()
    with pytest.raises(SafetyError):
        book.top(NOW_MS + 2001)
    assert not book.ready
    book = synced_book()
    book.disconnect()
    with pytest.raises(SafetyError):
        book.top(NOW_MS)


@pytest.mark.parametrize(
    "change",
    [
        {"U": 13, "u": 13, "pu": 12},
        {"U": 12, "u": 12, "pu": 9},
        {"E": NOW_MS - 2001},
        {"E": NOW_MS + 1},
        {"s": "ETHUSDCPERP"},
        {"U": "12"},
        {"u": 9, "U": 12},
        {"b": [["65020", "1"]]},
    ],
)
def test_bad_depth_halts_and_requires_new_snapshot(change):
    book = synced_book()
    frame = copy.deepcopy(DEPTH_FRAME)
    frame["data"].update({"U": 12, "u": 12, "pu": 11, **change})
    with pytest.raises(SafetyError):
        book.apply(frame, NOW_MS)
    assert not book.ready and book.last_id == -1


def test_zero_quantity_removes_level_and_duplicate_does_not_refresh():
    book = synced_book()
    assert book.bids[Decimal("65000")] == Decimal("0.2")
    book.apply(DEPTH_FRAME, NOW_MS + 100)
    assert book.last_received_ms == NOW_MS
    frame = copy.deepcopy(DEPTH_FRAME)
    frame["data"].update({"U": 12, "u": 12, "pu": 11, "b": [["65000", "0"], ["64990", "0.1"]]})
    book.apply(frame, NOW_MS)
    assert book.top(NOW_MS)[0] == Decimal("64990")


def test_risk_rejects_stale_unknown_inventory_overlapping_quotes_and_excess_notional(connected):
    market = connected[3]
    baseline = dict(
        position=Decimal("0"), position_observed_ms=NOW_MS, now_ms=NOW_MS, outstanding=[]
    )
    for overrides in [
        {"position": Decimal("NaN")},
        {"position": Decimal("0.02")},
        {"position_observed_ms": NOW_MS - 2001},
        {"outstanding": [object()]},
    ]:
        with pytest.raises(SafetyError):
            MakerPolicy().quotes(market, synced_book(), **{**baseline, **overrides})
    with pytest.raises(SafetyError):
        MakerPolicy(max_order_notional=Decimal("100")).quotes(market, synced_book(), **baseline)
