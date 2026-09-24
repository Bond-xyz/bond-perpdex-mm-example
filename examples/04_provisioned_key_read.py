"""Read one account using an already provisioned bot key and Ed25519 key.

This newer REST credential is different from the wallet session used by
examples 02 and 03. It does not replace wallet-signed order authorization.
"""

import httpx

from bond_perpdex.bot_key import BotKeyCredentials
from bond_perpdex.models import HTTP_URL


def main() -> None:
    credentials = BotKeyCredentials.from_environment()
    path = "/fapi/v1/account"
    headers = credentials.signed_headers("GET", path)
    with httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as client:
        response = client.get(HTTP_URL + path, headers=headers)
    print(f"GET {path}: HTTP {response.status_code}")
    response.raise_for_status()
    print("Account read succeeded; account balances are intentionally not printed.")


if __name__ == "__main__":
    main()
