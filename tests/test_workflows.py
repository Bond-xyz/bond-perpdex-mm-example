from decimal import Decimal
from types import SimpleNamespace

import pytest

from bond_perpdex_client import Quote, SafetyError, UnknownOutcome, UserStreamMessage
from bond_perpdex_client.offline import NOW_MS, OFFLINE_REGISTRY
from bond_perpdex_client.workflows import ConnectedAccount, place_and_cancel, watch_account


@pytest.mark.parametrize("failure", [None, "fail_after_accept", "fail_cancel"])
def test_supervised_flow_preserves_single_submission_and_cancel_semantics(
    connected, monkeypatch, failure
):
    client, venue, wallet, market = connected
    if failure:
        setattr(venue, failure, True)
    monkeypatch.setattr(
        "bond_perpdex_client.workflows.read_endpoint_time", lambda *args: NOW_MS // 1000
    )
    account = ConnectedAccount(client, wallet, "bond")

    def trade():
        return place_and_cancel(
            account,
            market.symbol,
            Quote("BUY", Decimal("65000"), Decimal("0.002")),
            rpc_url="https://unused.example",
            registry=OFFLINE_REGISTRY,
        )

    if failure:
        with pytest.raises(UnknownOutcome):
            trade()
        assert client.halted and len(client.uncertain_commands) == 1
    else:
        assert trade()["status"] == "CANCELED"
        assert client.open_orders(market.symbol) == []
    assert sum(method == "POST" and path == "/fapi/v1/order" for method, path in venue.calls) == 1
    assert sum(method == "DELETE" for method, _ in venue.calls) == (
        0 if failure == "fail_after_accept" else 1
    )


def test_nonterminal_projection_stops_supervised_flow_without_retry(connected, monkeypatch):
    client, venue, wallet, market = connected
    monkeypatch.setattr(
        "bond_perpdex_client.workflows.read_endpoint_time", lambda *args: NOW_MS // 1000
    )
    monkeypatch.setattr(client, "query_order", lambda *args, **kwargs: {"status": "NEW"})
    with pytest.raises(SafetyError, match="nonterminal"):
        place_and_cancel(
            ConnectedAccount(client, wallet, "bond"),
            market.symbol,
            Quote("BUY", Decimal("65000"), Decimal("0.002")),
            rpc_url="https://unused.example",
            registry=OFFLINE_REGISTRY,
        )
    assert sum(method == "DELETE" for method, _ in venue.calls) == 1


def test_failed_subscription_is_not_reported_as_a_normal_quiet_exit():
    def events(**kwargs):
        raise SafetyError("Subscription rejected")

    client = SimpleNamespace(user_events=events)
    with pytest.raises(SafetyError, match="Subscription rejected"):
        watch_account(client, "BTCUSDCPERP")


def test_failed_reconciliation_closes_stream_before_consuming_more_events():
    closed = []

    def events(**kwargs):
        try:
            yield UserStreamMessage("connected", 0, 1, True, "snapshot")
            pytest.fail("Must reconcile successfully before consuming another event")
        finally:
            closed.append(True)

    def reconcile(symbol):
        raise SafetyError("Account reader unavailable")

    client = SimpleNamespace(user_events=events, reconcile_account=reconcile)
    with pytest.raises(SafetyError, match="reader unavailable"):
        watch_account(client, "BTCUSDCPERP")
    assert closed == [True]
