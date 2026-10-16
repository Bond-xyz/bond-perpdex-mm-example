"""Offline managed-key header shape for trading REST routes; sends no requests.

Public Galileo account queries require SIWE. Managed credentials remain
REST-only and cannot authenticate the trading/private WebSocket.
"""

import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bond_perpdex_client.bot_key import BotKeyCredentials
from bond_perpdex_client.signing import signed_form


def main() -> None:
    key = Ed25519PrivateKey.generate()
    credentials = BotKeyCredentials(
        "bpd_testnet_example.synthetic-not-a-credential",
        key,
        "00000000-0000-4000-8000-000000000001",
    )
    body = signed_form(
        {
            "symbol": "BTCUSDCPERP",
            "orderId": "00000000-0000-4000-8000-000000000002",
            "newClientOrderId": "cancel-example-1",
            "timestamp": 1792137600000,
        },
        key,
    ).encode()
    headers = credentials.signed_headers(
        "DELETE", "/fapi/v1/order", body, timestamp_ms=1792137600000, nonce="example-nonce-1"
    )
    print(
        json.dumps(
            {
                "method": "DELETE",
                "path": "/fapi/v1/order",
                "headers": headers,
                "body": body.decode(),
            },
            indent=2,
        )
    )
    print("OFFLINE: synthetic credentials only; no request was sent.")


if __name__ == "__main__":
    main()
