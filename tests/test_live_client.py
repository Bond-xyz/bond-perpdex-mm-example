import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from urllib.parse import parse_qs, quote_plus

import httpx
import pytest

from bond_perpdex_client import (
    BondPerpDexClient,
    Quote,
    SafetyError,
    UnknownOutcome,
    WalletSigner,
)
from bond_perpdex_client import (
    TestnetConfig as Config,
)
from bond_perpdex_client import (
    TestnetTransport as LiveTransport,
)
from bond_perpdex_client.models import PreparedRequest
from bond_perpdex_client.offline import NOW_MS, OFFLINE_REGISTRY, OfflineVenue, demo_identity
from bond_perpdex_client.signing import canonical_form


@pytest.fixture
def live_mock(monkeypatch):
    venue = OfflineVenue()
    wire = []
    real_client = httpx.Client

    def handler(request):
        assert request.url.host == "perpdex-testnet.bond.xyz"
        wire.append(request)
        prepared = PreparedRequest(
            request.method,
            request.url.raw_path.decode(),
            request.content.decode(),
            dict(request.headers),
        )
        try:
            result = venue.request(prepared)
        except TimeoutError:
            raise httpx.ReadTimeout("Mocked lost acknowledgement") from None
        return httpx.Response(200, json=result)

    def factory(**kwargs):
        assert kwargs == {"timeout": 5, "follow_redirects": False, "trust_env": False}
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("bond_perpdex_client.transport.httpx.Client", factory)

    class CommandSocket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def send(self, raw):
            frame = json.loads(raw)
            assert frame["method"] == "openOrders.cancelAll"
            params = frame["params"]
            wire.append(
                httpx.Request("WS", "https://perpdex-testnet.bond.xyz/ws-fapi/v1", content=raw)
            )
            signed = (
                canonical_form(params) + "&signature=" + quote_plus(params["signature"], safe="")
            )
            self.result = venue.request(
                PreparedRequest(
                    "DELETE",
                    "/fapi/v1/openOrders",
                    signed,
                    {
                        "x-api-key": params["apiKey"],
                        "content-type": "application/x-www-form-urlencoded",
                    },
                )
            )
            self.id = frame["id"]

        def recv(self, timeout):
            assert 0 < timeout <= 10
            return json.dumps({"id": self.id, "status": 200, "result": self.result})

    monkeypatch.setattr(
        "bond_perpdex_client.transport.connect", lambda *args, **kwargs: CommandSocket()
    )
    config = Config(
        allow_live_private=True, allow_live_orders=True, signing_registry=OFFLINE_REGISTRY
    )
    transport = LiveTransport(config)
    client = BondPerpDexClient(transport, clock_ms=lambda: venue.now_ms)
    wallet, key = demo_identity()
    client.authenticate(wallet, key, "bond")
    return client, venue, wallet, client.market("BTCUSDCPERP"), wire


def prepare(context, *, side="BUY"):
    client, _, wallet, market, _ = context
    return client.prepare_order(
        wallet,
        market,
        Quote(side, Decimal("65000"), Decimal("0.002")),
        endpoint_time=NOW_MS // 1000,
    )


def test_real_transport_auth_place_account_reads_query_and_cancel_are_mocked(live_mock):
    client, venue, _, market, wire = live_mock
    assert [(request.method, request.url.path) for request in wire[:2]] == [
        ("GET", "/auth/nonce"),
        ("POST", "/auth/signin"),
    ]
    signin = json.loads(wire[1].content)
    assert signin["secret_type"] == "Ed25519" and "BEGIN PUBLIC KEY" in signin["secret_key"]
    assert client.account()["accountPresent"] is False
    assert client.balances() == [{"asset": "USDC.e", "balance": "0"}]
    assert client.commission_rate("BTCUSDCPERP") == {
        "symbol": "BTCUSDCPERP",
        "makerCommissionRate": "0",
        "takerCommissionRate": "0",
    }
    intent = prepare(live_mock)
    result = client.submit_order(intent)
    assert result["status"] == "NEW"
    assert client.query_order(market.symbol, intent.client_order_id)["orderId"] == result["orderId"]
    assert client.reconcile_account(market.symbol).positions[0]["positionAmt"] == "0"
    cancellation = client.cancel_order(market, result["orderId"])
    assert "status" not in cancellation
    assert client.pending_cancellations == (result["orderId"],)
    assert client.query_order(market.symbol, order_id=result["orderId"])["status"] == "CANCELED"
    assert not client.pending_cancellations
    assert client.open_orders(market.symbol) == []
    order_post = next(
        request
        for request in wire
        if request.method == "POST" and request.url.path == "/fapi/v1/order"
    )
    assert order_post.headers["content-type"] == "application/x-www-form-urlencoded"
    assert parse_qs(order_post.content.decode())["type"] == ["POST_ONLY"]
    assert venue.nonces


def test_cancel_all_exact_route_and_explicit_scope_confirmation(live_mock):
    client, _, _, market, wire = live_mock
    client.submit_order(prepare(live_mock))
    client.submit_order(prepare(live_mock, side="SELL"))
    with pytest.raises(SafetyError, match="confirmation"):
        client.cancel_all(market.symbol)
    result = client.cancel_all(market.symbol, confirm_all_for_symbol=True)
    assert len(result) == 2 and all(item["status"] == "CANCELED" for item in result)
    request = wire[-1]
    assert request.method == "WS" and request.url.path == "/ws-fapi/v1"
    fields = json.loads(request.content)["params"]
    assert set(fields) == {
        "symbol",
        "newClientOrderId",
        "timestamp",
        "recvWindow",
        "signature",
        "apiKey",
    }
    assert not any(r.method == "DELETE" and r.url.path == "/fapi/v1/openOrders" for r in wire)
    assert client.open_orders(market.symbol) == []


def test_lost_live_submit_response_halts_and_never_replays(live_mock):
    client, venue, _, _, wire = live_mock
    venue.fail_after_accept = True
    intent = prepare(live_mock)
    with pytest.raises(UnknownOutcome):
        client.submit_order(intent)
    assert intent.client_order_id in client.uncertain_commands
    assert len(venue.orders) == 1
    with pytest.raises(SafetyError):
        client.submit_order(intent)
    assert (
        sum(request.method == "POST" and request.url.path == "/fapi/v1/order" for request in wire)
        == 1
    )


def test_lost_cancel_all_response_blocks_later_sweeps(live_mock):
    client, venue, _, market, wire = live_mock
    client.submit_order(prepare(live_mock))
    venue.fail_cancel = True
    with pytest.raises(UnknownOutcome):
        client.cancel_all(market.symbol, confirm_all_for_symbol=True)
    assert client.halted and len(client.uncertain_commands) == 1
    with pytest.raises(SafetyError):
        client.cancel_all(market.symbol, confirm_all_for_symbol=True)
    assert sum(request.method == "WS" for request in wire) == 1


def test_lost_single_cancel_response_retains_command_and_never_retries(live_mock):
    client, venue, _, market, wire = live_mock
    ack = client.submit_order(prepare(live_mock))
    venue.fail_cancel = True
    with pytest.raises(UnknownOutcome):
        client.cancel_order(market, ack["orderId"])
    assert client.halted and len(client.uncertain_commands) == 1
    with pytest.raises(SafetyError):
        client.cancel_order(market, ack["orderId"])
    assert sum(request.method == "DELETE" for request in wire) == 1


def test_ordinary_cancel_ack_without_status_blocks_replacement_until_projection_is_terminal(
    live_mock,
):
    client, venue, _, market, _ = live_mock
    ack = client.submit_order(prepare(live_mock))
    result = client.cancel_order(market, ack["orderId"])
    assert "status" not in result and client.pending_cancellations
    replacement = prepare(live_mock)
    with pytest.raises(SafetyError, match="terminal order reconciliation"):
        client.submit_order(replacement)
    venue.orders[ack["orderId"]]["status"] = "NEW"
    client.reconcile_account(market.symbol)
    assert client.pending_cancellations
    with pytest.raises(SafetyError):
        client.submit_order(replacement)
    venue.orders[ack["orderId"]]["status"] = "CANCELED"
    client.reconcile_account(market.symbol)
    assert not client.pending_cancellations
    assert client.submit_order(replacement)["status"] == "NEW"


def test_stream_reconciliation_does_not_clear_uncertain_command_halt(live_mock):
    client, _, _, market, _ = live_mock
    intent = prepare(live_mock)
    client.stream_requires_reconciliation = True
    with pytest.raises(SafetyError, match="reconciliation"):
        client.submit_order(intent)
    client._stream_connected = True
    client.halted = True
    client.reconcile_account(market.symbol)
    assert not client.stream_requires_reconciliation and client.halted
    with pytest.raises(SafetyError):
        client.submit_order(intent)


def test_live_pinned_domain_guard_rejects_changed_intent_before_account_io(live_mock):
    client, _, _, _, wire = live_mock
    intent = prepare(live_mock)
    changed = intent.request.body.replace(
        "0x" + "22" * 20,
        "0x" + "11" * 20,
    )
    before = len(wire)
    with pytest.raises(SafetyError, match="pinned testnet domain"):
        client.submit_order(replace(intent, request=replace(intent.request, body=changed)))
    assert len(wire) == before


def test_live_preflight_caps_projected_inventory_and_pending_orders(live_mock):
    client, _, _, _, wire = live_mock
    intent = prepare(live_mock)
    client.config = replace(client.config, max_position="0.001")
    with pytest.raises(SafetyError, match="inventory"):
        client.submit_order(intent)
    assert not any(
        request.method == "POST" and request.url.path == "/fapi/v1/order" for request in wire
    )
    client.config = replace(client.config, max_position="0.010", max_open_orders=1)
    client.submit_order(intent)
    with pytest.raises(SafetyError, match="Open-order limit"):
        client.submit_order(prepare(live_mock, side="SELL"))


def test_session_only_permission_never_enables_order_commands(live_mock):
    client, _, wallet, market, _ = live_mock
    client.config = replace(client.config, allow_live_orders=False)
    with pytest.raises(SafetyError, match="not explicitly enabled"):
        client.prepare_order(wallet, market, Quote("BUY", Decimal("65000"), Decimal("0.002")))
    with pytest.raises(SafetyError):
        client.cancel_all(market.symbol, confirm_all_for_symbol=True)
    assert client.positions(market.symbol)


def test_real_transport_independent_flags_block_private_paths_before_io():
    with pytest.raises(SafetyError):
        LiveTransport().request(PreparedRequest("GET", "/auth/nonce", ""))
    with pytest.raises(SafetyError):
        LiveTransport().private_socket()
    with pytest.raises(SafetyError):
        Config(allow_live_orders=True).validate()
    with pytest.raises(SafetyError):
        LiveTransport(Config(allow_live_private=True, ws_api_url="wss://example.com/ws-fapi/v1"))
    transport = LiveTransport(Config(allow_live_private=True))
    with pytest.raises(SafetyError):
        transport.request(PreparedRequest("DELETE", "/fapi/v1/openOrders", ""))
    with pytest.raises(SafetyError):
        transport.request(PreparedRequest("GET", "https://other.example/auth/nonce", ""))


def test_credentials_are_explicit_memory_only_and_errors_do_not_echo_them(monkeypatch):
    monkeypatch.delenv("BOND_TESTNET_WALLET_KEY", raising=False)
    with pytest.raises(SafetyError):
        WalletSigner.from_environment()
    secret = hashlib.sha256(b"public unit-test credential only").hexdigest()
    monkeypatch.setenv("BOND_TESTNET_WALLET_KEY", secret)
    wallet = WalletSigner.from_environment()
    assert secret not in repr(wallet)
    invalid = "0" * 64
    monkeypatch.setenv("BOND_TESTNET_WALLET_KEY", invalid)
    with pytest.raises(SafetyError) as error:
        WalletSigner.from_environment()
    assert invalid not in str(error.value)


def test_live_auth_redirect_or_unknown_result_does_not_refresh_or_retry(monkeypatch):
    real_client = httpx.Client
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/auth/nonce":
            return httpx.Response(200, json={"nonce": "offlineNonce0001"})
        return httpx.Response(307, headers={"Location": "https://other.example/auth/signin"})

    monkeypatch.setattr(
        "bond_perpdex_client.transport.httpx.Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    client = BondPerpDexClient(
        LiveTransport(Config(allow_live_private=True)), clock_ms=lambda: NOW_MS
    )
    wallet, key = demo_identity()
    with pytest.raises(UnknownOutcome):
        client.authenticate(wallet, key, "bond")
    assert calls == ["/auth/nonce", "/auth/signin"]
    assert client._session is None and client.halted


@pytest.mark.parametrize("failure", ["wrong-id", "rejected", "bad-result", "timeout"])
def test_websocket_cancel_all_uncertainty_is_retained_without_rest_fallback(
    live_mock, monkeypatch, failure
):
    client, _, _, market, wire = live_mock
    calls = []

    class FailedSocket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def send(self, raw):
            self.frame = json.loads(raw)
            calls.append(self.frame)

        def recv(self, timeout):
            if failure == "timeout":
                raise TimeoutError("Lost acknowledgement")
            return json.dumps(
                {
                    "id": "other" if failure == "wrong-id" else self.frame["id"],
                    "status": 401 if failure == "rejected" else 200,
                    "result": {} if failure == "bad-result" else [],
                }
            )

    monkeypatch.setattr(
        "bond_perpdex_client.transport.connect", lambda *args, **kwargs: FailedSocket()
    )
    with pytest.raises(UnknownOutcome):
        client.cancel_all(market.symbol, confirm_all_for_symbol=True)
    assert client.halted and len(client.uncertain_commands) == 1
    with pytest.raises(SafetyError):
        client.cancel_all(market.symbol, confirm_all_for_symbol=True)
    assert len(calls) == 1
    assert not any(r.method == "DELETE" and r.url.path == "/fapi/v1/openOrders" for r in wire)


def test_live_order_requires_endpoint_clock_before_signing(live_mock):
    client, _, wallet, market, _ = live_mock
    with pytest.raises(SafetyError, match="Endpoint time"):
        client.prepare_order(wallet, market, Quote("BUY", Decimal("65000"), Decimal("0.002")))


@pytest.mark.parametrize("endpoint_time", [0, -1, True, 2**58])
def test_invalid_endpoint_clock_fails_before_signing(live_mock, endpoint_time):
    client, _, wallet, market, _ = live_mock
    with pytest.raises(SafetyError, match="Endpoint time"):
        client.prepare_order(
            wallet,
            market,
            Quote("BUY", Decimal("65000"), Decimal("0.002")),
            endpoint_time=endpoint_time,
        )


@pytest.mark.parametrize("clock_delta", [-3600, 0, 3600])
def test_signed_expiry_outlives_both_endpoint_and_ingress_clocks(live_mock, clock_delta):
    client, _, wallet, market, _ = live_mock
    endpoint_time = NOW_MS // 1000 + clock_delta
    intent = client.prepare_order(
        wallet,
        market,
        Quote("BUY", Decimal("65000"), Decimal("0.002")),
        endpoint_time=endpoint_time,
    )
    envelope = json.loads(parse_qs(intent.request.body)["signedOrder"][0])
    expiration = int(envelope["order"]["expiration"])
    assert expiration & ((1 << 58) - 1) == max(endpoint_time, NOW_MS // 1000) + 60
    assert expiration >> 62 == 3


def test_client_clock_overflow_fails_before_signing(live_mock):
    client, _, wallet, market, _ = live_mock
    client._session = replace(client._session, expires_at_ms=2**59 * 1000)
    client.clock_ms = lambda: 2**58 * 1000
    with pytest.raises(SafetyError, match="Client clock"):
        prepare(live_mock)
