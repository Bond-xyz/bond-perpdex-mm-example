from dataclasses import replace
from decimal import Decimal

import pytest

from bond_perpdex import (
    BondPerpDexClient,
    Quote,
    SafetyError,
    UnknownOutcome,
)
from bond_perpdex import (
    TestnetConfig as Config,
)
from bond_perpdex import (
    TestnetReadOnlyTransport as ReadOnly,
)
from bond_perpdex.models import PreparedRequest
from bond_perpdex.offline import demo_identity


def intent_for(connected, **kwargs):
    client, _, wallet, market = connected
    return client.prepare_order(
        wallet, market, Quote("BUY", Decimal("65000"), Decimal("0.002")), **kwargs
    )


def test_lost_submit_response_halts_without_retry_and_preserves_unknown_order(connected):
    client, venue, _, _ = connected
    intent = intent_for(connected)
    venue.fail_after_accept = True
    with pytest.raises(UnknownOutcome):
        client.submit_offline(intent)
    assert len(venue.orders) == 1
    calls = len(venue.calls)
    with pytest.raises(SafetyError):
        client.submit_offline(intent)
    with pytest.raises(SafetyError):
        intent_for(connected)
    assert len(venue.calls) == calls


def test_cancel_confirmed_and_unknown_cancel_is_not_retried(connected):
    client, venue, _, market = connected
    intent = intent_for(connected)
    client.submit_offline(intent)
    venue.fail_cancel = True
    with pytest.raises(UnknownOutcome):
        client.cancel_offline(market, intent.client_order_id)
    assert client.halted
    calls = len(venue.calls)
    with pytest.raises(SafetyError):
        client.cancel_offline(market, intent.client_order_id)
    assert len(venue.calls) == calls


def test_nonce_and_client_id_are_not_reused(connected):
    client, _, _, _ = connected
    intent = intent_for(connected, nonce=42)
    with pytest.raises(SafetyError):
        intent_for(connected, nonce=42)
    client.submit_offline(intent)
    with pytest.raises(SafetyError):
        client.submit_offline(intent)


@pytest.mark.parametrize("nonce", [0, -1, 2**64])
def test_nonce_uint64_bounds(connected, nonce):
    with pytest.raises(SafetyError):
        intent_for(connected, nonce=nonce)


def test_session_expiration_blocks_private_calls(connected):
    client, venue, _, _ = connected
    venue.now_ms += 86_400_000
    with pytest.raises(SafetyError):
        client.open_orders("BTCUSDCPERP")


@pytest.mark.parametrize(
    "config",
    [
        Config(chain_id=1),
        Config(http_url="https://example.com"),
        Config(http_url="http://perpdex-testnet.bond.xyz"),
        Config(http_url="https://perpdex-testnet.bond.xyz.evil"),
        Config(http_url="https://user:pass@perpdex-testnet.bond.xyz"),
        Config(ws_url="wss://example.com/ws"),
        Config(http_url="https://perpdex-testnet.bond.xyz/"),
    ],
)
def test_production_and_endpoint_overrides_rejected(config):
    with pytest.raises(SafetyError):
        BondPerpDexClient(config=config)


def test_live_auth_orders_cancels_and_private_reads_rejected_before_io(connected):
    client = BondPerpDexClient()
    wallet, key = demo_identity()
    intent = intent_for(connected)
    for call in [
        lambda: client.authenticate_offline(wallet, key),
        lambda: client.submit_offline(intent),
        lambda: client.cancel_offline(connected[3], intent.client_order_id),
        lambda: client.open_orders("BTCUSDCPERP"),
        lambda: client.positions("BTCUSDCPERP"),
        lambda: client.prepare_order(
            wallet, connected[3], Quote("BUY", Decimal("65000"), Decimal("0.002"))
        ),
    ]:
        with pytest.raises(SafetyError):
            call()


@pytest.mark.parametrize(
    "wire_request",
    [
        PreparedRequest("POST", "/auth/signin", "{}"),
        PreparedRequest("DELETE", "/fapi/v1/order", ""),
        PreparedRequest("GET", "/auth/nonce", ""),
        PreparedRequest("GET", "/fapi/v1/exchangeInfo", "", {"x-api-key": "never-send"}),
        PreparedRequest("GET", "//evil.test/fapi/v1/exchangeInfo", ""),
        PreparedRequest("GET", "/fapi/v1/exchangeInfo?apiKey=never-send", ""),
        PreparedRequest("GET", "/fapi/v1/depth?symbol=BTCUSDCPERP&limit=20&symbol=ETHUSDCPERP", ""),
    ],
)
def test_transport_independently_blocks_unsafe_requests(wire_request):
    with pytest.raises(SafetyError):
        ReadOnly().request(wire_request)


def test_wrong_order_domain_cannot_be_signed(connected):
    client, _, wallet, market = connected
    with pytest.raises(SafetyError):
        client.prepare_order(
            wallet,
            replace(market, virtual_book="0x" + "11" * 20),
            Quote("BUY", Decimal("65000"), Decimal("0.002")),
        )


def test_signatures_and_keys_are_not_in_object_repr(connected):
    client, _, wallet, _ = connected
    intent = intent_for(connected)
    for value in [client, wallet, client._session, intent, intent.request]:
        assert "offline-session-not-a-credential" not in repr(value)
        assert "signature=" not in repr(value)
