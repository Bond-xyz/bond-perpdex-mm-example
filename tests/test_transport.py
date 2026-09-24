import json

import httpx
import pytest

from bond_perpdex_client import BondPerpDexClient, SafetyError
from bond_perpdex_client import TestnetReadOnlyTransport as ReadOnly
from bond_perpdex_client.models import PreparedRequest
from bond_perpdex_client.offline import DEPTH_FRAME, EXCHANGE_INFO


def install_http_mock(monkeypatch, handler):
    real_client = httpx.Client

    def client(**kwargs):
        assert kwargs == {"timeout": 5, "follow_redirects": False, "trust_env": False}
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("bond_perpdex_client.transport.httpx.Client", client)


def test_public_http_exact_origin_and_shape(monkeypatch):
    def handler(request):
        assert str(request.url) == "https://perpdex-testnet.bond.xyz/fapi/v1/exchangeInfo"
        assert "x-api-key" not in request.headers
        return httpx.Response(200, json=EXCHANGE_INFO)

    install_http_mock(monkeypatch, handler)
    assert BondPerpDexClient().market("BTCUSDCPERP").product_id == 2


@pytest.mark.parametrize("status", [301, 302, 401, 429, 500, 503])
def test_public_http_errors_and_redirects_never_retry(monkeypatch, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://example.com"}, json={})

    install_http_mock(monkeypatch, handler)
    with pytest.raises(SafetyError):
        ReadOnly().request(PreparedRequest("GET", "/fapi/v1/time", ""))
    assert len(calls) == 1


def test_websocket_subscription_ack_and_wrapped_depth(monkeypatch):
    class Socket:
        def __init__(self):
            self.frames = iter([{"result": None, "id": "mm-depth"}, DEPTH_FRAME])

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def send(self, raw):
            assert json.loads(raw) == {
                "method": "SUBSCRIBE",
                "params": ["btcusdcperp@depth@100ms"],
                "id": "mm-depth",
            }

        def recv(self, timeout):
            assert timeout == 5
            return json.dumps(next(self.frames))

    def connect(url, **kwargs):
        assert url == "wss://perpdex-testnet.bond.xyz/ws"
        assert kwargs["proxy"] is None and kwargs["max_queue"] == 16
        return Socket()

    monkeypatch.setattr("bond_perpdex_client.transport.connect", connect)
    assert list(ReadOnly().depth_events("BTCUSDCPERP")) == [DEPTH_FRAME]


def test_websocket_disconnect_propagates_without_reconnect(monkeypatch):
    calls = []

    def connect(*args, **kwargs):
        calls.append(args)
        raise ConnectionError("simulated disconnect")

    monkeypatch.setattr("bond_perpdex_client.transport.connect", connect)
    with pytest.raises(ConnectionError):
        list(ReadOnly().depth_events("BTCUSDCPERP"))
    assert len(calls) == 1
