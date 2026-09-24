"""Verify the two transport signatures stay distinct and the examples import."""

import base64
import hashlib
import json
from decimal import Decimal
from urllib.parse import parse_qs

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bond_perpdex_client import BondPerpDexClient, Quote, websocket_order_place
from bond_perpdex_client.bot_key import BotKeyCredentials
from bond_perpdex_client.offline import NOW_MS, OfflineVenue, demo_identity
from bond_perpdex_client.signing import canonical_form, compact


def test_ws_order_place_reuses_the_rest_wallet_and_account_signatures():
    wallet, key = demo_identity()
    client = BondPerpDexClient(OfflineVenue(), clock_ms=lambda: NOW_MS)
    client.authenticate_offline(wallet, key)
    intent = client.prepare_order(
        wallet, client.market("BTCUSDCPERP"), Quote("BUY", Decimal("65000"), Decimal("0.002"))
    )
    frame = websocket_order_place(intent, request_id="oems-1")
    params = frame["params"]
    assert frame["id"] == "oems-1" and frame["method"] == "order.place"
    assert isinstance(params["timestamp"], int)
    assert params["signedOrder"]["hashes"]["digest"] == intent.digest
    assert params["apiKey"] == intent.request.headers["x-api-key"]
    encoded = {key: values[0] for key, values in parse_qs(intent.request.body).items()}
    assert compact(params["signedOrder"]) == encoded["signedOrder"]
    assert params["signature"] == encoded["signature"]
    key.public_key().verify(
        base64.b64decode(params["signature"]),
        canonical_form(params).encode(),
    )


def test_bot_key_header_signature_binds_body_and_subaccount():
    key = Ed25519PrivateKey.from_private_bytes(bytes([7]) * 32)
    account = "00000000-0000-4000-8000-000000000001"
    credentials = BotKeyCredentials("bpd_test.fixture", key, account)
    body = json.dumps({"symbol": "BTCUSDCPERP"}, separators=(",", ":")).encode()
    headers = credentials.signed_headers(
        "POST", "/fapi/v1/order", body, timestamp_ms=NOW_MS, nonce="oems-nonce-1"
    )
    signed = (
        f"{NOW_MS}\noems-nonce-1\nPOST\n/fapi/v1/order\n{account}\n"
        f"{hashlib.sha256(body).hexdigest()}"
    ).encode()
    key.public_key().verify(base64.b64decode(headers["x-bond-signature"]), signed)
    assert headers["x-bond-subaccount-id"] == account
    assert "bpd_test.fixture" not in repr(credentials)
