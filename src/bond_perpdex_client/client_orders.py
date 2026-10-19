"""Signed order preparation, preflight, submission, and cancellation."""

import json
import secrets
import uuid
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from urllib.parse import parse_qs

from .models import (
    SUPPORTED_SYMBOLS,
    Json,
    Market,
    OrderIntent,
    PriceBand,
    Quote,
    SafetyError,
    Side,
    UnknownOutcome,
    decimal,
    text,
    units,
)
from .signing import (
    OrderType,
    TimeInForce,
    WalletSigner,
    compact,
    decode_expiration,
    encode_expiration,
)

# Conservative fallback when exchangeInfo omits execution-band metadata.
DEFAULT_FAIR_PRICE_BAND_BPS = 300
# POST_ONLY rests; LIMIT/GTC may fill on entry; IOC/FOK may already be terminal.
_IMMEDIATE = frozenset({"NEW", "PARTIALLY_FILLED", "FILLED", "EXPIRED", "CANCELED"})
ACKNOWLEDGED_STATUSES = {
    ("POST_ONLY", "GTC"): frozenset({"NEW"}),
    ("LIMIT", "GTC"): frozenset({"NEW", "PARTIALLY_FILLED", "FILLED"}),
    ("LIMIT", "IOC"): _IMMEDIATE,
    ("LIMIT", "FOK"): _IMMEDIATE,
}


def fair_price_band(
    market: Market, band_bps: int = DEFAULT_FAIR_PRICE_BAND_BPS, *, crossing: bool = True
) -> PriceBand:
    """Return the inclusive band an order must satisfy, as index-price multipliers.

    A crossing (marketable) order uses the PERCENT_PRICE execution band, intersected
    with the resting band it must also satisfy; a resting order uses the resting band.
    Without execution fields, crossing orders use ``band_bps``, intersected with any
    advertised resting band. A resting band alone does not describe the execution
    limit. Without a filter, both order kinds use ``band_bps``.
    """
    resting, execution = market.resting_band, market.execution_band
    if not crossing and resting is not None:
        return resting
    if crossing and execution is not None:
        selected = execution
    else:
        if type(band_bps) is not int or not 0 < band_bps < 10_000:
            raise SafetyError("Fair-price band must be 1-9999 basis points")
        fraction = Decimal(band_bps) / 10_000
        selected = PriceBand(1 - fraction, 1 + fraction)
    if resting is None:
        return selected
    return PriceBand(max(resting.down, selected.down), min(resting.up, selected.up))


def marketable_limit_price(
    market: Market,
    side: Side,
    index_price: Decimal,
    *,
    worst_price: Decimal | None = None,
    band_bps: int = DEFAULT_FAIR_PRICE_BAND_BPS,
) -> Decimal:
    """Tick-aligned LIMIT/IOC price bounded by ``worst_price`` and the execution band.

    BUY: floor-to-tick(min(worst, index * up)); SELL: ceil-to-tick(max(worst, index * down)).
    See ``fair_price_band`` for the execution, resting and configured band fallback.
    Without ``worst_price`` the band edge is the slippage bound. A worst price on the
    far side of the band cannot be admitted and is rejected rather than moved.
    """
    if side not in {"BUY", "SELL"}:
        raise SafetyError("Side must be BUY or SELL")
    if not isinstance(index_price, Decimal) or not index_price.is_finite() or index_price <= 0:
        raise SafetyError("Index price must be a positive Decimal")
    if worst_price is not None and (
        not isinstance(worst_price, Decimal) or not worst_price.is_finite() or worst_price <= 0
    ):
        raise SafetyError("Worst acceptable price must be a positive Decimal")
    band = fair_price_band(market, band_bps, crossing=True)
    with localcontext() as ctx:
        ctx.prec = 80
        lower, upper = index_price * band.down, index_price * band.up
        if side == "BUY":
            bound = upper if worst_price is None else min(worst_price, upper)
            price = (bound / market.tick).to_integral_value(ROUND_FLOOR) * market.tick
        else:
            bound = lower if worst_price is None else max(worst_price, lower)
            price = (bound / market.tick).to_integral_value(ROUND_CEILING) * market.tick
    if not lower <= price <= upper or price <= 0:
        raise SafetyError(
            "Worst acceptable price is outside the fair-price band "
            "(the venue would reject PRICE_OUTSIDE_FAIR_PRICE_BAND)"
        )
    return price


class OrderCommands:
    """Mutation methods; the composed client supplies sessions and account reads."""

    def prepare_order(
        self,
        wallet: WalletSigner,
        market: Market,
        quote: Quote,
        *,
        subaccount: str | None = None,
        endpoint_time: int | None = None,
        nonce: int | None = None,
        client_order_id: str | None = None,
        order_type: OrderType = "POST_ONLY",
        time_in_force: TimeInForce = "GTC",
        reduce_only: bool = False,
    ) -> OrderIntent:
        """Sign one order. The default POST_ONLY/GTC mode matches earlier releases.

        LIMIT/IOC and LIMIT/FOK may be reduce-only. Signed MARKET is unsupported; use
        ``prepare_marketable_order`` for a band-bounded LIMIT/IOC instead.
        """
        with self._lock:
            self._permission(mutation=True)
            if self.halted:
                raise SafetyError("Client halted; resolve unknown outcomes before restarting")
            session = self._active_session()
            subaccount = session.subaccount if subaccount is None else subaccount
            if self.signing_registry is None or (
                market.product_id,
                market.virtual_book,
            ) != self.signing_registry.markets.get(market.symbol):
                raise SafetyError("Order domain differs from the verified signing registry")
            if wallet.address != session.owner or subaccount != session.subaccount:
                raise SafetyError("Wallet and subaccount must match the authenticated session")
            market.validate_order(quote.price, quote.quantity)
            if quote.price * quote.quantity > decimal(self.config.max_order_notional):
                raise SafetyError("Order exceeds client notional limit")
            nonce = nonce if nonce is not None else secrets.randbits(64) | 1
            if nonce in self._nonces:
                raise SafetyError("Order nonce was already allocated; it cannot be reused")
            if endpoint_time is None:
                if not self.transport.offline:
                    raise SafetyError("Read verified Endpoint time before preparing a live order")
                endpoint_time = self.clock_ms() // 1000
            if type(endpoint_time) is not int or not 0 < endpoint_time < 2**58 - 60:
                raise SafetyError(
                    "Endpoint time must be a positive timestamp fitting the signed expiry"
                )
            # Ingress also checks wall time. Endpoint time can lag between batches.
            ingress_time = self.clock_ms() // 1000
            if not 0 < ingress_time < 2**58 - 60:
                raise SafetyError("Client clock must fit the signed expiry")
            expires_at = max(endpoint_time, ingress_time) + 60
            encode_expiration(order_type, time_in_force, reduce_only, expires_at)
            self._nonces.add(nonce)
            envelope = wallet.order(
                market,
                quote.side,
                quote.price,
                quote.quantity,
                subaccount,
                expires_at,
                nonce,
                order_type=order_type,
                time_in_force=time_in_force,
                reduce_only=reduce_only,
            )
            client_id = client_order_id or str(uuid.uuid4())
            uuid.UUID(client_id)
            fields = {
                "symbol": market.symbol,
                "side": quote.side,
                "type": order_type,
                "timeInForce": time_in_force,
                # Top-level price must equal signed priceX18 / 1e18 exactly.
                "price": text(quote.price),
                "quantity": text(quote.quantity),
                "newClientOrderId": client_id,
                "signedOrder": compact(envelope),
            }
            if reduce_only:
                fields["reduceOnly"] = "true"
            request = self._private("POST", "/fapi/v1/order", fields)
            return OrderIntent(client_id, envelope["hashes"]["digest"], request)

    def prepare_marketable_order(
        self,
        wallet: WalletSigner,
        market: Market,
        side: Side,
        quantity: Decimal,
        *,
        worst_price: Decimal | None = None,
        reduce_only: bool = False,
        index_price: Decimal | None = None,
        band_bps: int = DEFAULT_FAIR_PRICE_BAND_BPS,
        subaccount: str | None = None,
        endpoint_time: int | None = None,
        nonce: int | None = None,
        client_order_id: str | None = None,
    ) -> OrderIntent:
        """Sign a LIMIT/IOC at a band-clamped worst acceptable price; it does not submit.

        Reads public ``premiumIndex`` for ``indexPrice`` unless ``index_price`` is given.
        """
        if index_price is None:
            index_price = self.index_price(market.symbol)
        price = marketable_limit_price(
            market, side, index_price, worst_price=worst_price, band_bps=band_bps
        )
        return self.prepare_order(
            wallet,
            market,
            Quote(side, price, quantity),
            subaccount=subaccount,
            endpoint_time=endpoint_time,
            nonce=nonce,
            client_order_id=client_order_id,
            order_type="LIMIT",
            time_in_force="IOC",
            reduce_only=reduce_only,
        )

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
        if (
            self.signing_registry is None
            or fields.get("symbol") not in self.signing_registry.markets
        ):
            raise SafetyError("Load the verified signing registry for this market")
        product_id, virtual_book = self.signing_registry.markets[fields["symbol"]]
        envelope = json.loads(fields["signedOrder"])
        order_type, time_in_force, reduce_only, _ = decode_expiration(
            int(envelope["order"]["expiration"])
        )
        if (fields.get("type"), fields.get("timeInForce"), fields.get("reduceOnly", "false")) != (
            order_type,
            time_in_force,
            "true" if reduce_only else "false",
        ):
            raise SafetyError("Request type, timeInForce or reduceOnly differs from signed bits")
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
            body = parse_qs(intent.request.body)
            mode = (body.get("type", [""])[0], body.get("timeInForce", [""])[0])
            accepted = ACKNOWLEDGED_STATUSES.get(mode, frozenset())
            try:
                result = self._object(self._send(intent.request))
                uuid.UUID(result["orderId"])
                if (
                    result.get("clientOrderId") != intent.client_order_id
                    or result.get("status") not in accepted
                ):
                    raise SafetyError("Unexpected order acknowledgement requires reconciliation")
                if result["status"] in {"NEW", "PARTIALLY_FILLED"}:
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
            if self.signing_registry is None or (
                market.product_id,
                market.virtual_book,
            ) != self.signing_registry.markets.get(market.symbol):
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
            if confirm_all_for_symbol is not True or symbol not in SUPPORTED_SYMBOLS:
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
            send = (
                self._send
                if self.transport.offline
                else getattr(self.transport, "trading_request", None)
            )
            if not callable(send):
                raise SafetyError("This transport does not support signed WebSocket cancel-all")
            self._attempted.add(client_order_id)
            self._uncertain_cancel_all.add(symbol)
            self.uncertain_commands[client_order_id] = request
            try:
                results = self._objects(send(request))
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
