"""Signed order preparation, preflight, submission, and cancellation."""

import json
import secrets
import uuid
from urllib.parse import parse_qs

from .models import (
    VIRTUAL_BOOKS,
    Json,
    Market,
    OrderIntent,
    Quote,
    SafetyError,
    UnknownOutcome,
    decimal,
    text,
    units,
)
from .signing import WalletSigner, compact


class OrderCommands:
    """Mutation methods; the composed client supplies sessions and account reads."""

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
