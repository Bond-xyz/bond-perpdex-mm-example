"""Reusable client separate from strategy, credentials, and command-line execution."""

import json
import secrets
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import parse_qs

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .models import (
    VIRTUAL_BOOKS,
    AccountSnapshot,
    Json,
    Market,
    OrderIntent,
    PreparedRequest,
    Quote,
    SafetyError,
    TestnetConfig,
    UnknownOutcome,
    UserStreamMessage,
    decimal,
    text,
    units,
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


class BondPerpDexClient:
    """Testnet client with explicit private/mutation permissions and no mutation retries."""

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

    def positions(self, symbol: str) -> list[Json]:
        return self._objects(
            self._send(self._private("GET", "/fapi/v1/positionRisk", {"symbol": symbol}))
        )

    def open_orders(self, symbol: str) -> list[Json]:
        return self._objects(
            self._send(self._private("GET", "/fapi/v1/openOrders", {"symbol": symbol}))
        )

    @property
    def pending_cancellations(self) -> tuple[str, ...]:
        return tuple(self._pending_cancels)

    def query_order(
        self,
        symbol: str,
        client_order_id: str | None = None,
        *,
        order_id: str | None = None,
    ) -> Json:
        if (client_order_id is None) == (order_id is None):
            raise SafetyError("Query exactly one client order ID or server order UUID")
        if order_id is not None:
            uuid.UUID(order_id)
        result = self._object(
            self._send(
                self._private(
                    "GET",
                    "/fapi/v1/order",
                    {
                        "symbol": symbol,
                        "origClientOrderId": client_order_id,
                        "orderId": order_id,
                    },
                )
            )
        )

        if result.get("symbol") != symbol or (
            order_id is not None and result.get("orderId") != order_id
        ):
            raise SafetyError("Order query returned a different identity")
        if client_order_id is not None and result.get("clientOrderId") != client_order_id:
            raise SafetyError("Order query returned a different client order ID")
        if result.get("status") in {"CANCELED", "FILLED", "EXPIRED"}:
            terminal_id = result["orderId"]
            self._pending_cancels.pop(terminal_id, None)
            self._open = {key: value for key, value in self._open.items() if value != terminal_id}
        return result

    def reconcile_account(self, symbol: str) -> AccountSnapshot:
        started = self.clock_ms()
        stream_revision = self._stream_revision
        for order_id, market in tuple(self._pending_cancels.items()):
            if market == symbol:
                self.query_order(symbol, order_id=order_id)
        positions, orders = self.positions(symbol), self.open_orders(symbol)
        if not 0 <= self.clock_ms() - started <= 5000:
            raise SafetyError("Account snapshot reads exceeded the freshness bound")
        if len(positions) != 1 or positions[0].get("symbol") != symbol:
            raise SafetyError("Missing or ambiguous account inventory")
        decimal(positions[0]["positionAmt"])
        seen = set()
        for order in orders:
            uuid.UUID(order["orderId"])
            if order["orderId"] in seen or order.get("symbol") != symbol:
                raise SafetyError("Duplicate or wrong-market open order")
            seen.add(order["orderId"])
            if not 0 <= decimal(order["executedQty"]) <= decimal(order["origQty"]):
                raise SafetyError("Invalid open-order quantities")
        result = AccountSnapshot(symbol, positions, orders, self.clock_ms())
        if self._stream_connected and stream_revision == self._stream_revision:
            self.stream_requires_reconciliation = False
        return result

    def user_events(
        self,
        *,
        max_events: int = 100,
        max_reconnects: int = 2,
    ) -> Iterator[UserStreamMessage]:
        from .user_stream import user_events

        return user_events(self, max_events=max_events, max_reconnects=max_reconnects)

    def prepare_order(
        self,
        wallet: WalletSigner,
        market: Market,
        quote: Quote,
        *,
        subaccount: str = "bond",
        nonce: int | None = None,
        client_order_id: str | None = None,
    ) -> OrderIntent:
        with self._lock:
            self._permission(mutation=True)
            if self.halted:
                raise SafetyError("Client halted; resolve unknown outcomes before restarting")
            session = self._active_session()
            if wallet.address != session.owner or subaccount != session.subaccount:
                raise SafetyError("Wallet and subaccount must match the authenticated session")
            market.validate_order(quote.price, quote.quantity)
            if quote.price * quote.quantity > decimal(self.config.max_order_notional):
                raise SafetyError("Order exceeds client notional limit")
            nonce = nonce if nonce is not None else secrets.randbits(64) | 1
            if nonce in self._nonces:
                raise SafetyError("Order nonce was already allocated; it cannot be reused")
            self._nonces.add(nonce)
            envelope = wallet.order(
                market,
                quote.side,
                quote.price,
                quote.quantity,
                subaccount,
                self.clock_ms() // 1000 + 60,
                nonce,
            )
            client_id = client_order_id or str(uuid.uuid4())
            uuid.UUID(client_id)
            request = self._private(
                "POST",
                "/fapi/v1/order",
                {
                    "symbol": market.symbol,
                    "side": quote.side,
                    "type": "POST_ONLY",
                    "timeInForce": "GTC",
                    "price": text(quote.price),
                    "quantity": text(quote.quantity),
                    "newClientOrderId": client_id,
                    "signedOrder": compact(envelope),
                },
            )
            return OrderIntent(client_id, envelope["hashes"]["digest"], request)

    def submit_offline(self, intent: OrderIntent) -> Json:
        self._offline()
        return self.submit_order(intent)

    def _live_preflight(self, intent: OrderIntent) -> None:
        session = self._active_session()
        if self._pending_cancels:
            raise SafetyError("Cancellation acknowledgement needs terminal order reconciliation")
        if self.stream_requires_reconciliation:
            raise SafetyError("Private stream requires REST reconciliation before new orders")
        if intent.request.headers.get("x-api-key") != session.api_key:
            raise SafetyError("Prepared order belongs to a different session")
        fields = {key: values[0] for key, values in parse_qs(intent.request.body).items()}
        if fields.get("type") != "POST_ONLY" or fields.get("timeInForce") != "GTC":
            raise SafetyError("Only POST_ONLY/GTC placement is supported")
        if fields.get("symbol") not in VIRTUAL_BOOKS:
            raise SafetyError("Unknown testnet market")
        product_id, virtual_book = VIRTUAL_BOOKS[fields["symbol"]]
        envelope = json.loads(fields["signedOrder"])
        if (
            envelope["domain"]
            != {
                "name": "Vertex",
                "version": "0.0.1",
                "chainId": self.config.chain_id,
                "verifyingContract": virtual_book,
            }
            or envelope["productId"] != product_id
        ):
            raise SafetyError("Prepared order differs from the pinned testnet domain")
        if (
            fields["newClientOrderId"] != intent.client_order_id
            or envelope["hashes"]["digest"] != intent.digest
        ):
            raise SafetyError("Prepared order identity changed")
        if int(envelope["order"]["priceX18"]) != units(decimal(fields["price"]), 18):
            raise SafetyError("Prepared signed price differs from transport")
        snapshot = self.reconcile_account(fields["symbol"])
        matches = [
            position
            for position in snapshot.positions
            if position.get("symbol") == fields["symbol"]
        ]
        if len(matches) != 1 or matches[0].get("positionSide", "BOTH") != "BOTH":
            raise SafetyError("Missing, ambiguous or unsupported inventory")
        position = decimal(matches[0]["positionAmt"])
        if len(snapshot.open_orders) >= self.config.max_open_orders:
            raise SafetyError("Open-order limit reached")
        ids = {order["orderId"] for order in snapshot.open_orders}
        if not set(self._open.values()) <= ids:
            raise SafetyError("Account projection has not reconciled acknowledged orders")
        buys, sells = decimal("0"), decimal("0")
        for order in [
            *snapshot.open_orders,
            {**fields, "origQty": fields["quantity"], "executedQty": "0"},
        ]:
            if order["symbol"] != fields["symbol"] or order["side"] not in {"BUY", "SELL"}:
                raise SafetyError("Unexpected open-order market or side")
            remaining = decimal(order["origQty"]) - decimal(order["executedQty"])
            if remaining < 0:
                raise SafetyError("Invalid remaining order quantity")
            if order["side"] == "BUY":
                buys += remaining
            else:
                sells += remaining
        maximum = decimal(self.config.max_position)
        if max(abs(position), abs(position + buys), abs(position - sells)) > maximum:
            raise SafetyError("Worst-case inventory exceeds the client position limit")
        if decimal(fields["price"]) * decimal(fields["quantity"]) > decimal(
            self.config.max_order_notional
        ):
            raise SafetyError("Order exceeds the client notional limit")
        if not 0 <= self.clock_ms() - int(fields["timestamp"]) <= 5000:
            raise SafetyError("Prepared request expired during preflight; it was not submitted")

    def submit_order(self, intent: OrderIntent) -> Json:
        with self._lock:
            self._permission(mutation=True)
            if self.halted or intent.client_order_id in self._attempted:
                raise SafetyError("No automatic replay, retry, or submission while halted")
            if (intent.request.method, intent.request.path) != ("POST", "/fapi/v1/order"):
                raise SafetyError("An order intent must use the exact order placement route")
            if not self.transport.offline:
                self._live_preflight(intent)
            self._attempted.add(intent.client_order_id)
            self.uncertain_commands[intent.client_order_id] = intent.request
            try:
                result = self._object(self._send(intent.request))
                uuid.UUID(result["orderId"])
                if (
                    result.get("clientOrderId") != intent.client_order_id
                    or result.get("status") != "NEW"
                ):
                    raise SafetyError("Unexpected order acknowledgement requires reconciliation")
                self._open[intent.client_order_id] = result["orderId"]
                del self.uncertain_commands[intent.client_order_id]
                return result
            except Exception:
                self.halted = True
                raise UnknownOutcome(
                    "Submit outcome unknown; retain intent and stop, never retry"
                ) from None

    def cancel_offline(self, market: Market, client_order_id: str) -> Json:
        self._offline()
        order_id = self._open.get(client_order_id)
        if order_id is None:
            raise SafetyError("Only an acknowledged order owned by this client may be cancelled")
        result = self.cancel_order(market, order_id)
        if result.get("status") is None:
            result = self.query_order(market.symbol, order_id=order_id)
            if result.get("status") not in {"CANCELED", "FILLED", "EXPIRED"}:
                raise SafetyError("Offline cancellation has not reached a terminal projection")
        return result

    def cancel_order(
        self,
        market: Market,
        order_id: str,
        *,
        client_order_id: str | None = None,
    ) -> Json:
        with self._lock:
            self._permission(mutation=True)
            uuid.UUID(order_id)
            if (market.product_id, market.virtual_book) != VIRTUAL_BOOKS.get(market.symbol):
                raise SafetyError("Unknown testnet market")
            if order_id in self._cancel_attempted:
                raise SafetyError("Cancellation was already attempted; no automatic retry")
            client_order_id = client_order_id or str(uuid.uuid4())
            uuid.UUID(client_order_id)
            if client_order_id in self._attempted:
                raise SafetyError("Command client ID was already used")
            request = self._private(
                "DELETE",
                "/fapi/v1/order",
                {
                    "productId": market.product_id,
                    "orderId": order_id,
                    "newClientOrderId": client_order_id,
                },
            )
            self._cancel_attempted.add(order_id)
            self._attempted.add(client_order_id)
            self.uncertain_commands[client_order_id] = request
            try:
                result = self._object(self._send(request))
                if result.get("orderId") != order_id or result.get("symbol") != market.symbol:
                    raise SafetyError("Cancellation not confirmed")
                if result.get("status") == "CANCELED":
                    self._open = {
                        key: value for key, value in self._open.items() if value != order_id
                    }
                elif (
                    result.get("status") is None and result.get("clientOrderId") == client_order_id
                ):
                    self._pending_cancels[order_id] = market.symbol
                else:
                    raise SafetyError("Unexpected cancellation acknowledgement")
                del self.uncertain_commands[client_order_id]
                return result
            except Exception:
                self.halted = True
                raise UnknownOutcome(
                    "Cancel outcome unknown; no replacement or automatic retry"
                ) from None

    def cancel_all(
        self,
        symbol: str,
        *,
        confirm_all_for_symbol: bool = False,
        client_order_id: str | None = None,
    ) -> list[Json]:
        with self._lock:
            self._permission(mutation=True)
            if confirm_all_for_symbol is not True or symbol not in VIRTUAL_BOOKS:
                raise SafetyError("Explicit whole-symbol cancellation confirmation is required")
            if symbol in self._uncertain_cancel_all:
                raise SafetyError("Previous cancel-all is uncertain; do not sweep or retry again")
            client_order_id = client_order_id or str(uuid.uuid4())
            uuid.UUID(client_order_id)
            if client_order_id in self._attempted:
                raise SafetyError("Command client ID was already used")
            request = self._private(
                "DELETE",
                "/fapi/v1/openOrders",
                {
                    "symbol": symbol,
                    "newClientOrderId": client_order_id,
                },
            )
            self._attempted.add(client_order_id)
            self._uncertain_cancel_all.add(symbol)
            self.uncertain_commands[client_order_id] = request
            try:
                results = self._objects(self._send(request))
                ids = set()
                for result in results:
                    uuid.UUID(result["orderId"])
                    if result.get("status") != "CANCELED" or result.get("symbol") != symbol:
                        raise SafetyError("Cancel-all acknowledgement is incomplete")
                    ids.add(result["orderId"])
                self._open = {key: value for key, value in self._open.items() if value not in ids}
                self._pending_cancels = {
                    key: value for key, value in self._pending_cancels.items() if key not in ids
                }
                self._uncertain_cancel_all.remove(symbol)
                del self.uncertain_commands[client_order_id]
                return results
            except Exception:
                self.halted = True
                raise UnknownOutcome(
                    "Cancel-all outcome uncertain; no retry or replacement"
                ) from None

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
