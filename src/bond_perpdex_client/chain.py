"""Read-only Galileo chain/time preflight for wallet-signed order expiry."""

from urllib.parse import urlsplit

import httpx
from eth_utils import keccak

from .models import CHAIN_ID, GALILEO_RPC_URL, SafetyError
from .registry import SigningRegistry


def read_endpoint_time(rpc_url: str | None, registry: SigningRegistry) -> int:
    """Read Endpoint.getTime() after checking chain 16602.

    An empty or ``None`` URL selects the public Galileo RPC; pass your own to override.
    """
    rpc_url = rpc_url or GALILEO_RPC_URL
    parsed = urlsplit(rpc_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise SafetyError("Supply a verified HTTPS Galileo RPC URL")
    with httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as client:

        def call(method, params):
            response = client.post(
                rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            )
            response.raise_for_status()
            data = response.json()
            if data.get("error") or not isinstance(data.get("result"), str):
                raise SafetyError("Galileo chain/time read failed; no order was prepared")
            return data["result"]

        if int(call("eth_chainId", []), 16) != CHAIN_ID:
            raise SafetyError("RPC is not Galileo chain 16602")
        result = call(
            "eth_call",
            [
                {"to": registry.endpoint, "data": "0x" + keccak(text="getTime()")[:4].hex()},
                "latest",
            ],
        )
        if len(result) != 66 or not result.startswith("0x") or not 0 < int(result, 16) < 2**58:
            raise SafetyError("Endpoint getTime() returned an invalid timestamp")
        return int(result, 16)
