"""One-level inventory-bounded quotes, deliberately independent of the client."""

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from .models import Json, Market, Quote, SafetyError, decimal


class DepthBook:
    def __init__(self, symbol: str, max_age_ms: int = 2000):
        if not 1 <= max_age_ms <= 10_000:
            raise SafetyError("Invalid freshness bound")
        self.symbol = symbol
        self.max_age_ms = max_age_ms
        self.disconnect()

    def disconnect(self) -> None:
        self.ready = False
        self.last_id = -1
        self.last_received_ms = -1
        self.last_event_ms = -1
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}

    @staticmethod
    def _levels(target: dict[Decimal, Decimal], levels: list[list[str]]) -> None:
        for level in levels:
            if len(level) != 2:
                raise SafetyError("Invalid depth level")
            price, quantity = map(decimal, level)
            if price <= 0 or quantity < 0:
                raise SafetyError("Invalid depth price or quantity")
            if quantity == 0:
                target.pop(price, None)
            else:
                target[price] = quantity

    def snapshot(self, snapshot: Json) -> None:
        self.disconnect()
        try:
            if type(snapshot["lastUpdateId"]) is not int or snapshot["lastUpdateId"] < 0:
                raise SafetyError("Invalid snapshot sequence")
            self._levels(self.bids, snapshot["bids"])
            self._levels(self.asks, snapshot["asks"])
            self.last_id = snapshot["lastUpdateId"]
        except Exception:
            self.disconnect()
            raise

    def apply(self, frame: Json, now_ms: int) -> None:
        try:
            if frame["stream"] != self.symbol.lower() + "@depth@100ms":
                raise SafetyError("Wrong depth stream")
            event = frame["data"]
            if event["e"] != "depthUpdate" or event["s"] != self.symbol:
                raise SafetyError("Wrong depth event")
            if any(type(event[key]) is not int or event[key] < 0 for key in ("U", "u", "pu", "E")):
                raise SafetyError("Invalid depth sequence or timestamp")
            if event["U"] > event["u"] or not 0 <= now_ms - event["E"] <= self.max_age_ms:
                raise SafetyError("Stale or invalid depth event")
            if event["u"] <= self.last_id:
                return
            if self.last_id < 0 or not event["U"] <= self.last_id + 1 <= event["u"]:
                raise SafetyError("Depth gap: discard state and resnapshot")
            if (self.ready and event["pu"] != self.last_id) or event["pu"] > self.last_id:
                raise SafetyError("Depth predecessor mismatch")
            self._levels(self.bids, event["b"])
            self._levels(self.asks, event["a"])
            if not self.bids or not self.asks or max(self.bids) >= min(self.asks):
                raise SafetyError("Empty or crossed book")
            self.last_id = event["u"]
            self.last_received_ms = now_ms
            self.last_event_ms = event["E"]
            self.ready = True
        except Exception:
            self.disconnect()
            raise

    def top(self, now_ms: int) -> tuple[Decimal, Decimal]:
        if not self.ready or any(
            not 0 <= now_ms - timestamp <= self.max_age_ms
            for timestamp in (self.last_received_ms, self.last_event_ms)
        ):
            self.disconnect()
            raise SafetyError("Disconnected, unsynchronized or stale book: cancel, never quote")
        return max(self.bids), min(self.asks)


@dataclass(frozen=True)
class MakerPolicy:
    quantity: Decimal = Decimal("0.002")
    half_spread_bps: int = 10
    max_position: Decimal = Decimal("0.010")
    max_order_notional: Decimal = Decimal("200")

    def quotes(
        self,
        market: Market,
        book: DepthBook,
        *,
        position: Decimal,
        position_observed_ms: int,
        now_ms: int,
        outstanding: list[Quote],
    ) -> list[Quote]:
        if not position.is_finite() or not 0 <= now_ms - position_observed_ms <= book.max_age_ms:
            raise SafetyError("Unknown or stale inventory")
        if not 1 <= self.half_spread_bps <= 1000 or not all(
            value.is_finite() and value > 0
            for value in (
                self.quantity,
                self.max_position,
                self.max_order_notional,
            )
        ):
            raise SafetyError("Invalid risk policy")
        if outstanding:
            raise SafetyError("Cancel/reconcile every previous quote before replacing; no overlap")
        if abs(position) > self.max_position:
            raise SafetyError("Inventory breached; stop rather than invent a liquidation strategy")
        bid, ask = book.top(now_ms)
        mid = (bid + ask) / 2
        offset = mid * self.half_spread_bps / 10_000
        prices = {
            "BUY": min(
                bid, ((mid - offset) / market.tick).to_integral_value(ROUND_FLOOR) * market.tick
            ),
            "SELL": max(
                ask, ((mid + offset) / market.tick).to_integral_value(ROUND_CEILING) * market.tick
            ),
        }
        result = []
        for side, price in prices.items():
            worst_position = position + (self.quantity if side == "BUY" else -self.quantity)
            if abs(worst_position) > self.max_position:
                continue
            market.validate_order(price, self.quantity)
            if price * self.quantity > self.max_order_notional:
                raise SafetyError("Order exceeds local notional cap")
            result.append(Quote(side, price, self.quantity))
        return result
