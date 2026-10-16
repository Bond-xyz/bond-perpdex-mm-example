import json

import httpx
import pytest
from eth_utils import keccak

from bond_perpdex_client import SafetyError, read_endpoint_time
from bond_perpdex_client.offline import NOW_MS, OFFLINE_REGISTRY


def rpc_mock(monkeypatch, chain_id=16602, timestamp=None):
    calls = []
    real_client = httpx.Client

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert str(request.url) == "https://rpc.example"
        result = hex(chain_id) if payload["method"] == "eth_chainId" else timestamp
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    def factory(**kwargs):
        assert kwargs == {"timeout": 5, "follow_redirects": False, "trust_env": False}
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("bond_perpdex_client.chain.httpx.Client", factory)
    return calls


def test_endpoint_time_is_read_from_the_verified_contract_after_chain_check(monkeypatch):
    calls = rpc_mock(monkeypatch, timestamp=f"0x{NOW_MS // 1000:064x}")
    assert read_endpoint_time("https://rpc.example", OFFLINE_REGISTRY) == NOW_MS // 1000
    assert [c["method"] for c in calls] == ["eth_chainId", "eth_call"]
    assert calls[1]["params"] == [
        {"to": OFFLINE_REGISTRY.endpoint, "data": "0x" + keccak(text="getTime()")[:4].hex()},
        "latest",
    ]


def test_wrong_chain_stops_before_reading_an_endpoint(monkeypatch):
    calls = rpc_mock(monkeypatch, chain_id=1)
    with pytest.raises(SafetyError, match="not Galileo"):
        read_endpoint_time("https://rpc.example", OFFLINE_REGISTRY)
    assert len(calls) == 1


@pytest.mark.parametrize("value", ["0x00", "0x" + "00" * 32, "0x" + "ff" * 32, None])
def test_invalid_or_unavailable_endpoint_time_fails_without_order_submission(monkeypatch, value):
    rpc_mock(monkeypatch, timestamp=value)
    with pytest.raises(SafetyError):
        read_endpoint_time("https://rpc.example", OFFLINE_REGISTRY)


@pytest.mark.parametrize(
    "url",
    ["http://rpc.example", "https://user:password@rpc.example", "https://rpc.example#fragment"],
)
def test_insecure_or_ambiguous_rpc_urls_fail_before_network(url):
    with pytest.raises(SafetyError):
        read_endpoint_time(url, OFFLINE_REGISTRY)
