"""Private account reads and reconciliation after stream or command uncertainty."""

import uuid
from collections.abc import Iterator

from .models import AccountSnapshot, Json, SafetyError, UserStreamMessage, decimal


class AccountQueries:
    """Read-side operations; the composed client supplies signed transport."""

    def account(self) -> Json:
        """Return the venue's account projection, including collateral balances."""
        return self._object(self._send(self._private("GET", "/fapi/v1/account", {})))

    def balances(self) -> list[Json]:
        """Return the venue's balance rows without changing their wire fields."""
        return self._objects(self._send(self._private("GET", "/fapi/v1/balance", {})))

    def commission_rate(self, symbol: str) -> Json:
        """Return the account's advertised maker and taker rates for a market."""
        return self._object(
            self._send(self._private("GET", "/fapi/v1/commissionRate", {"symbol": symbol}))
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
