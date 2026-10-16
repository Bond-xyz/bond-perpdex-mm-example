"""Session lifecycle, permissions, transport, and shared request helpers."""

import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .credentials import Session, SessionCache
from .models import (
    SUPPORTED_SYMBOLS,
    Json,
    Market,
    PreparedRequest,
    SafetyError,
    TestnetConfig,
    UnknownOutcome,
)
from .registry import SigningRegistry
from .signing import WalletSigner, canonical_form, compact, signed_form
from .transport import TestnetReadOnlyTransport, Transport


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
        self.signing_registry = config.signing_registry or (
            getattr(self.transport, "signing_registry", None) if self.transport.offline else None
        )
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
        info = self.exchange_info()
        if not self.transport.offline:
            self.discover_signing_context(info)
        if self.signing_registry is None:
            raise SafetyError("Load a verified signing registry before selecting a trading market")
        return Market.from_exchange_info(info, symbol, self.signing_registry)

    def discover_signing_context(self, info: Json | None = None) -> SigningRegistry:
        """Load public signing metadata, rejecting a changed mapping or explicit pin mismatch."""
        try:
            discovered = SigningRegistry.from_exchange_info(
                self.exchange_info() if info is None else info
            )
        except (ValueError, KeyError, TypeError) as error:
            self.halted = True
            raise SafetyError(f"Invalid exchangeInfo signing context: {error}") from None
        current = self.signing_registry
        if current is not None and (
            current.endpoint != discovered.endpoint or current.markets != discovered.markets
        ):
            self.halted = True
            raise SafetyError(
                "exchangeInfo signing context changed or differs from the configured pin"
            )
        self.signing_registry = discovered
        return discovered

    def depth(self, symbol: str) -> Json:
        if symbol not in SUPPORTED_SYMBOLS:
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
        subaccount: str | None = None,
    ) -> None:
        self._permission()
        if not self.transport.offline and (subaccount is None or key is None):
            raise SafetyError(
                "Live sign-in requires an explicit subaccount and persistent Ed25519 key"
            )
        subaccount = subaccount if subaccount is not None else "bond"
        from .signing import subaccount_sender

        subaccount_sender(wallet.address, subaccount)
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
            self._session = Session(
                result["api_key"], key, expiry, wallet.address, subaccount, result["id"]
            )
        except Exception:
            self.halted = True
            raise UnknownOutcome(
                "Sign-in outcome uncertain; old credentials discarded, no retry"
            ) from None

    def authenticate_cached(
        self,
        wallet: WalletSigner,
        key: Ed25519PrivateKey,
        subaccount: str,
        session_file: str | Path,
    ) -> None:
        """Reuse a matching token or explicitly renew within 24 hours of expiry.

        Call at startup or a coordinated renewal boundary, after closing streams
        and reconciling uncertain commands. This is not a background renewer.
        """
        self._permission()
        if self._stream_connected or self.uncertain_commands or self._pending_cancels:
            raise SafetyError("Close streams and reconcile pending commands before session renewal")
        cache = SessionCache(
            session_file,
            self.config.http_url,
            self.config.chain_id,
            wallet.address,
            subaccount,
            key,
        )
        candidate = cache.load()
        if candidate is not None and not candidate.needs_renewal(self.clock_ms()):
            me = self._object(
                self._send(PreparedRequest("GET", "/auth/me", "", {"x-api-key": candidate.api_key}))
            )
            cache.validate_identity(me, candidate)
            self._session = candidate
            return
        self.authenticate(wallet, key, subaccount)
        cache.save(self._active_session())

    def _private(self, method: str, path: str, fields: Json) -> PreparedRequest:
        session = self._active_session()
        if method != "GET":
            self._permission(mutation=True)
        if fields.get("symbol") is not None and fields["symbol"] not in SUPPORTED_SYMBOLS:
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
