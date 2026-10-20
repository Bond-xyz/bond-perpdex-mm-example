"""Exact-origin public and explicitly enabled private testnet transports."""

import json
from collections.abc import Iterator
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

import httpx
from websockets.sync.client import ClientConnection, connect

from .models import SUPPORTED_SYMBOLS, Json, PreparedRequest, SafetyError, TestnetConfig

PUBLIC_PATHS = frozenset(
    {"/fapi/v1/time", "/fapi/v1/exchangeInfo", "/fapi/v1/depth", "/fapi/v1/premiumIndex"}
)


class Transport(Protocol):
    offline: bool

    def request(self, request: PreparedRequest) -> object: ...


class TestnetReadOnlyTransport:
    offline = False
    private_enabled = False
    mutations_enabled = False

    def __init__(self, config: TestnetConfig | None = None):
        config = config or TestnetConfig()
        config.validate()
        self.config = config

    def request(self, request: PreparedRequest) -> object:
        self.config.validate()
        parsed = urlsplit(request.path)
        allowed = PUBLIC_PATHS
        if (
            request.method != "GET"
            or request.body
            or request.headers
            or parsed.path not in allowed
            or parsed.scheme
            or parsed.netloc
            or parsed.fragment
        ):
            raise SafetyError("Live transport permits only unauthenticated testnet market reads")
        params = parse_qs(parsed.query, keep_blank_values=True)
        if parsed.path == "/fapi/v1/depth":
            if set(params) != {"symbol", "limit"} or params["limit"] != ["20"]:
                raise SafetyError("Unsupported depth parameters")
            if len(params["symbol"]) != 1 or params["symbol"][0] not in SUPPORTED_SYMBOLS:
                raise SafetyError("Unsupported depth market")
        elif parsed.path == "/fapi/v1/premiumIndex":
            if set(params) != {"symbol"} or len(params["symbol"]) != 1:
                raise SafetyError("Unsupported premiumIndex parameters")
            if params["symbol"][0] not in SUPPORTED_SYMBOLS:
                raise SafetyError("Unsupported premiumIndex market")
        elif params:
            raise SafetyError("Unexpected public query parameters")
        return self._http(request)

    def _http(self, request: PreparedRequest) -> object:
        self.config.validate()
        with httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as client:
            with client.stream(
                request.method,
                self.config.http_url + request.path,
                content=request.body.encode(),
                headers=request.headers,
            ) as response:
                if response.status_code != 200:
                    raise SafetyError(f"HTTP response status {response.status_code}; no retry")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > 1_000_000:
                        raise SafetyError("Public response exceeds size bound")
        return json.loads(body)

    def depth_events(self, symbol: str, count: int = 1) -> Iterator[Json]:
        self.config.validate()
        if symbol not in SUPPORTED_SYMBOLS or not 1 <= count <= 100:
            raise SafetyError("Unsupported stream or bounded event count")
        stream = symbol.lower() + "@depth@100ms"
        with connect(
            self.config.ws_url,
            proxy=None,
            ssl=httpx.create_ssl_context(trust_env=False),
            open_timeout=5,
            close_timeout=2,
            max_size=1_000_000,
            max_queue=16,
        ) as socket:
            socket.send(json.dumps({"method": "SUBSCRIBE", "params": [stream], "id": "mm-depth"}))
            delivered = 0
            acknowledged = False
            for _ in range(count + 10):
                frame = json.loads(socket.recv(timeout=5))
                if frame.get("id") == "mm-depth":
                    if frame.get("result", "missing") is not None:
                        raise SafetyError("Depth subscription rejected")
                    acknowledged = True
                    continue
                if not acknowledged:
                    raise SafetyError("Depth event arrived before subscription acknowledgement")
                if frame.get("stream") != stream or frame.get("data", {}).get("s") != symbol:
                    raise SafetyError("Unexpected stream payload")
                yield frame
                delivered += 1
                if delivered == count:
                    return
            raise SafetyError("No bounded depth stream progress")


class TestnetTransport(TestnetReadOnlyTransport):
    """Explicitly enabled legacy-session REST and private-stream transport; never retries."""

    @property
    def private_enabled(self) -> bool:
        return self.config.allow_live_private

    @property
    def mutations_enabled(self) -> bool:
        return self.config.allow_live_orders

    def request(self, request: PreparedRequest) -> object:
        self.config.validate()
        parsed = urlsplit(request.path)
        if parsed.path in PUBLIC_PATHS:
            return super().request(request)
        if not self.private_enabled or parsed.scheme or parsed.netloc or parsed.fragment:
            raise SafetyError("Private testnet I/O is not explicitly enabled or path is invalid")
        route = (request.method, parsed.path)
        if route in {("GET", "/auth/nonce"), ("POST", "/auth/signin")}:
            if parsed.query or "x-api-key" in request.headers:
                raise SafetyError("Unexpected authentication transport fields")
            if request.method == "GET" and (request.body or request.headers):
                raise SafetyError("Nonce request must be unauthenticated")
            if request.method == "POST":
                body = json.loads(request.body)
                if set(body) != {"message", "signature", "secret_type", "secret_key"}:
                    raise SafetyError("Unexpected sign-in fields")
                if body["secret_type"] != "Ed25519" or "BEGIN PUBLIC KEY" not in body["secret_key"]:
                    raise SafetyError("Only a public Ed25519 session key may be transported")
            return self._http(request)
        if route == ("GET", "/auth/me"):
            if parsed.query or request.body or not request.headers.get("x-api-key"):
                raise SafetyError("Identity reads require only a session header")
            return self._http(request)
        reads = {
            ("GET", path)
            for path in (
                "/fapi/v1/openOrders",
                "/fapi/v1/positionRisk",
                "/fapi/v1/order",
                "/fapi/v1/account",
                "/fapi/v1/balance",
                "/fapi/v1/commissionRate",
            )
        }
        commands = {
            ("POST", "/fapi/v1/order"),
            ("DELETE", "/fapi/v1/order"),
        }
        if route not in reads | commands or (route in commands and not self.mutations_enabled):
            raise SafetyError("Testnet command path or live-mutation permission denied")
        if not request.headers.get("x-api-key"):
            raise SafetyError("A session API key is required")
        if request.method == "GET" and request.body:
            raise SafetyError("Private GET must use query fields, not a body")
        if request.method != "GET" and (
            parsed.query
            or request.headers.get("content-type") != "application/x-www-form-urlencoded"
        ):
            raise SafetyError("Private commands require a form body")
        encoded = parsed.query if request.method == "GET" else request.body
        fields = parse_qs(encoded, keep_blank_values=True, strict_parsing=True)
        if any(len(value) != 1 for value in fields.values()) or not all(
            fields.get(key, [""])[0] for key in ("timestamp", "signature")
        ):
            raise SafetyError("Missing signature/timestamp or duplicate form fields")
        return self._http(request)

    def private_socket(self) -> ClientConnection:
        self.config.validate()
        if not self.private_enabled:
            raise SafetyError("Private testnet WebSocket is not explicitly enabled")
        return connect(
            self.config.ws_api_url,
            proxy=None,
            ssl=httpx.create_ssl_context(trust_env=False),
            open_timeout=5,
            close_timeout=2,
            max_size=1_000_000,
            max_queue=16,
        )

    def trading_request(self, request: PreparedRequest) -> object:
        """One bounded signed cancel-all call; never replay on socket failure.

        The example opens one socket per sweep. An OEMS should reuse a bounded
        command connection and correlate responses, respecting upgrade limits.
        """
        from .user_stream import _receive
        from .wire import websocket_cancel_all

        self.config.validate()
        if not self.mutations_enabled:
            raise SafetyError("Trading WebSocket commands require explicit live-order permission")
        frame = websocket_cancel_all(request)
        with self.private_socket() as socket:
            socket.send(json.dumps(frame))
            response = _receive(socket, timeout=10)
            if (
                response.get("id") != frame["id"]
                or response.get("status") != 200
                or "error" in response
            ):
                raise SafetyError("Trading WebSocket command failed; reconcile before retrying")
            if not isinstance(response.get("result"), list):
                raise SafetyError("Unexpected cancel-all response; reconcile before retrying")
            return response["result"]
