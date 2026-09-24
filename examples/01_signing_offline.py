"""Inspect both order signatures and the WebSocket shape without network I/O.

Run after ``sh install.sh``: .venv/bin/python examples/01_signing_offline.py
All identities and market data in this script are synthetic fixtures.
"""

import base64
import json
from decimal import Decimal
from urllib.parse import parse_qs

from bond_perpdex_client import BondPerpDexClient, Quote, websocket_order_place
from bond_perpdex_client.offline import NOW_MS, OfflineVenue, demo_identity
from bond_perpdex_client.signing import canonical_form


def main() -> None:
    wallet, ed25519_key = demo_identity()
    venue = OfflineVenue()
    client = BondPerpDexClient(venue, clock_ms=lambda: NOW_MS)
    client.authenticate_offline(wallet, ed25519_key)
    market = client.market("BTCUSDCPERP")

    # A SIWE challenge establishes the Ed25519 session. It is not an order nonce.
    siwe = wallet.signin("offlineNonce0001", ed25519_key, "bond", NOW_MS)
    intent = client.prepare_order(
        wallet, market, Quote("BUY", Decimal("65000"), Decimal("0.002")), nonce=42
    )
    fields = {name: values[0] for name, values in parse_qs(intent.request.body).items()}
    envelope = json.loads(fields["signedOrder"])
    ed25519_key.public_key().verify(
        base64.b64decode(fields["signature"]),
        canonical_form(fields).encode(),
    )
    ws = websocket_order_place(intent, request_id="offline-order-1")

    print("SIWE message (synthetic):\n" + siwe["message"])
    print("Ed25519 public key PEM:\n" + siwe["secret_key"])
    print("EIP-712 domain and signed order (synthetic):")
    print(json.dumps(envelope, indent=2))
    print("REST POST /fapi/v1/order and WS order.place use the same signedOrder digest:")
    print(ws["method"], ws["params"]["signedOrder"]["hashes"]["digest"])
    print("No request was sent.")


if __name__ == "__main__":
    main()
