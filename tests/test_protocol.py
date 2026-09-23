import base64
import copy
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from eth_account import Account
from eth_account.messages import encode_defunct

from bond_perpdex import Quote, SafetyError, UnknownOutcome
from bond_perpdex.offline import NOW_MS, demo_identity
from bond_perpdex.signing import (
    canonical_form,
    compact,
    order_hashes,
    signed_form,
    subaccount_sender,
    typed_message,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_source_eip712_golden_hashes_and_recovery():
    golden = json.loads((FIXTURES / "order-vector.json").read_text())
    assert order_hashes(golden["domain"], golden["order"]) == golden["hashes"]
    recovered = Account.recover_message(
        typed_message(golden["domain"], golden["order"]),
        signature=golden["signature"],
    )
    assert recovered.lower() == golden["signer"].lower()
    altered = copy.deepcopy(golden["order"])
    altered["nonce"] = "43"
    assert order_hashes(golden["domain"], altered)["digest"] != golden["hashes"]["digest"]


def test_exact_rust_browser_percent_encoded_vector():
    golden = json.loads((FIXTURES / "order-vector.json").read_text())
    golden["signer"] = golden["signer"].lower()
    actual = canonical_form({"signedOrder": compact(golden)})
    expected = "signedOrder=" + (FIXTURES / "signed-order-form.txt").read_text().strip()
    assert actual == expected
    assert canonical_form({"b": "a b~*+", "a": False, "z": None}) == "a=false&b=a+b%7E*%2B"


def test_ed25519_signs_sorted_form_not_raw_json_or_signature_field():
    key = Ed25519PrivateKey.from_private_bytes(bytes([42]) * 32)
    fields = {"timestamp": 1770000000000, "symbol": "BTCUSDCPERP", "reduceOnly": False}
    wire = signed_form(fields, key)
    decoded = parse_qs(wire)
    signature = base64.b64decode(decoded["signature"][0])
    key.public_key().verify(signature, canonical_form(fields).encode())
    with pytest.raises(InvalidSignature):
        key.public_key().verify(signature, canonical_form({**fields, "reduceOnly": True}).encode())


def test_siwe_personal_sign_binds_public_ed25519_key():
    wallet, key = demo_identity()
    body = wallet.signin("offlineNonce0001", key, "bond", NOW_MS)
    assert body["secret_type"] == "Ed25519"
    assert "BEGIN PUBLIC KEY" in body["secret_key"]
    assert key.public_key().public_bytes_raw().hex() in body["message"]
    assert "Chain ID: 16602" in body["message"]
    assert (
        Account.recover_message(encode_defunct(text=body["message"]), signature=body["signature"])
        == wallet.address
    )


@pytest.mark.parametrize("side,sign", [("BUY", 1), ("SELL", -1)])
def test_signed_order_exact_terms_and_post_only_flags(connected, side, sign):
    client, venue, wallet, market = connected
    intent = client.prepare_order(
        wallet, market, Quote(side, Decimal("65000"), Decimal("0.002")), nonce=2**64 - 1
    )
    fields = parse_qs(intent.request.body)
    envelope = json.loads(fields["signedOrder"][0])
    order = envelope["order"]
    assert fields["type"] == ["POST_ONLY"] and fields["timeInForce"] == ["GTC"]
    assert order["priceX18"] == "65000000000000000000000"
    assert int(order["amount"]) == sign * 2_000_000_000_000_000
    assert order["nonce"] == str(2**64 - 1)
    assert int(order["expiration"]) == (NOW_MS // 1000 + 60) | (3 << 62)
    signature = bytes.fromhex(envelope["signature"][2:])
    n = int("fffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141", 16)
    assert int.from_bytes(signature[32:64]) <= n // 2
    assert signature[64] in {27, 28}
    assert client.submit_offline(intent)["status"] == "NEW"
    assert len(venue.orders) == 1


@pytest.mark.parametrize(
    "price,quantity",
    [("65000.01", "0.002"), ("65000", "0.0021"), ("65000", "0.001"), ("NaN", "0.002")],
)
def test_precision_and_minimum_notional_fail_closed(connected, price, quantity):
    client, _, wallet, market = connected
    with pytest.raises(SafetyError):
        client.prepare_order(wallet, market, Quote("BUY", Decimal(price), Decimal(quantity)))


def test_subaccount_encoding_and_invalid_names():
    wallet, _ = demo_identity()
    assert (
        subaccount_sender(wallet.address, "bond") == wallet.address.lower() + "626f6e64" + "00" * 8
    )
    for name in ["", "x" * 13, "é", "a\nb"]:
        with pytest.raises(SafetyError):
            subaccount_sender(wallet.address, name)


def test_outer_transport_signature_tampering_is_rejected(connected):
    client, venue, _, _ = connected
    intent = client.prepare_order(
        connected[2], connected[3], Quote("BUY", Decimal("65000"), Decimal("0.002"))
    )
    changed = replace(
        intent.request, body=intent.request.body.replace("price=65000", "price=65001")
    )
    with pytest.raises(UnknownOutcome):
        client.submit_offline(replace(intent, request=changed))
    assert not venue.orders


def test_valid_outer_signature_does_not_hide_wrong_inner_signed_terms(connected):
    client, venue, _, _ = connected
    intent = client.prepare_order(
        connected[2], connected[3], Quote("BUY", Decimal("65000"), Decimal("0.002"))
    )
    fields = {name: values[0] for name, values in parse_qs(intent.request.body).items()}
    fields["quantity"] = "0.003"
    _, key = demo_identity()
    changed = replace(intent.request, body=signed_form(fields, key))
    with pytest.raises(UnknownOutcome):
        client.submit_offline(replace(intent, request=changed))
    assert not venue.orders
