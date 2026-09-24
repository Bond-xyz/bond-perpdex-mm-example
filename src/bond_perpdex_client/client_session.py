"""Session lifecycle, permissions, transport, and shared request helpers."""

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .models import (
    VIRTUAL_BOOKS,
    Json,
    Market,
    PreparedRequest,
    SafetyError,
    TestnetConfig,
    UnknownOutcome,
)
from .signing import WalletSigner, canonical_form, compact, signed_form
from .transport import TestnetReadOnlyTransport, Transport


@dataclass(repr=False)
class Session:
    api_key: str = field(repr=False)
    key: Ed25519PrivateKey = field(repr=False)
    expires_at_ms: int
    owner: str
    subaccount: str


class SessionClient:
    """Own the credential, command state, and exact signed transport."""

    def __init__(
        self,
        transport: Transport | None = None,
        *,
        config: TestnetConfig | None = None,
        clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
    ):
        config = config or getattr(transport, "config", None) or TestnetConfig()
        config.validate()
        self.config = config
        self.transport = transport if transport is not None else TestnetReadOnlyTransport(config)
        self.clock_ms = clock_ms
        self._session: Session | None = None
        self._lock = threading.RLock()
        self._nonces: set[int] = set()
        self._attempted: set[str] = set()
        self._cancel_attempted: set[str] = set()
        self._open: dict[str, str] = {}
        self._pending_cancels: dict[str, str] = {}
        self._uncertain_cancel_all: set[str] = set()
        self.uncertain_commands: dict[str, PreparedRequest] = {}
        self.stream_requires_reconciliation = False
        self._stream_connected = False
        self._stream_revision = 0
        self.halted = False

    def _offline(self) -> None:
        self.config.validate()
        if not self.transport.offline:
            raise SafetyError("This compatibility method is restricted to offline transports")

    def _permission(self, *, mutation: bool = False) -> None:
        self.config.validate()
        if self.transport.offline:
            return
        if not self.config.allow_live_private or not getattr(
            self.transport, "private_enabled", False
        ):
            raise SafetyError("Live private access is not explicitly enabled")
        if mutation and (
            not self.config.allow_live_orders
            or not getattr(self.transport, "mutations_enabled", False)
        ):
            raise SafetyError("Live order commands are not explicitly enabled")

    def _active_session(self) -> Session:
        self._permission()
        if self._session is None or self._session.expires_at_ms <= self.clock_ms():
            raise SafetyError("A current session is required; no automatic SIWE refresh")
        return self._session

    def _send(self, request: PreparedRequest) -> object:
        self.config.validate()
        return self.transport.request(request)

    def exchange_info(self) -> Json:
        return self._object(self._send(PreparedRequest("GET", "/fapi/v1/exchangeInfo", "")))

    def market(self, symbol: str) -> Market:
        return Market.from_exchange_info(self.exchange_info(), symbol)

    def depth(self, symbol: str) -> Json:
        if symbol not in VIRTUAL_BOOKS:
            raise SafetyError("Unsupported testnet market")
        query = canonical_form({"symbol": symbol, "limit": 20})
        return self._object(self._send(PreparedRequest("GET", "/fapi/v1/depth?" + query, "")))

    def authenticate_offline(
        self,
        wallet: WalletSigner,
        key: Ed25519PrivateKey,
        subaccount: str = "bond",
    ) -> None:
        self._offline()
        self.authenticate(wallet, key, subaccount)

    def authenticate(
        self,
        wallet: WalletSigner,
        key: Ed25519PrivateKey | None = None,
        subaccount: str = "bond",
    ) -> None:
        self._permission()
        key = key or Ed25519PrivateKey.generate()
        self._session = None
        nonce = self._object(self._send(PreparedRequest("GET", "/auth/nonce", "")))["nonce"]
        body = wallet.signin(nonce, key, subaccount, self.clock_ms())
        try:
            result = self._object(
                self._send(
                    PreparedRequest(
                        "POST",
                        "/auth/signin",
                        compact(body),
                        {"content-type": "application/json"},
                    )
                )
            )
            expiry_time = datetime.fromisoformat(result["expires_at"])
            if expiry_time.tzinfo is None:
                raise SafetyError("Session expiry must include a timezone")
            expiry = int(expiry_time.timestamp() * 1000)
            uuid.UUID(result["id"])
            if (
                not isinstance(result.get("api_key"), str)
                or not result["api_key"]
                or expiry <= self.clock_ms()
            ):
                raise SafetyError("Invalid sign-in response")
            self._session = Session(result["api_key"], key, expiry, wallet.address, subaccount)
        except Exception:
            self.halted = True
            raise UnknownOutcome(
                "Sign-in outcome uncertain; old credentials discarded, no retry"
            ) from None

    def _private(self, method: str, path: str, fields: Json) -> PreparedRequest:
        session = self._active_session()
        if method != "GET":
            self._permission(mutation=True)
        if fields.get("symbol") is not None and fields["symbol"] not in VIRTUAL_BOOKS:
            raise SafetyError("Unsupported testnet market")
        body = signed_form(
            {**fields, "timestamp": self.clock_ms(), "recvWindow": 5000}, session.key
        )
        headers = {"x-api-key": session.api_key}
        if method == "GET":
            return PreparedRequest(method, path + "?" + body, "", headers)
        return PreparedRequest(
            method,
            path,
            body,
            {
                **headers,
                "content-type": "application/x-www-form-urlencoded",
            },
        )

    @staticmethod
    def _object(value: object) -> Json:
        if not isinstance(value, dict):
            raise SafetyError("Expected a JSON object")
        return value

    @staticmethod
    def _objects(value: object) -> list[Json]:
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise SafetyError("Expected a JSON object list")
        return value
