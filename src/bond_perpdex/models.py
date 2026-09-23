"""Small public value types; all monetary arithmetic uses integers or Decimal."""

from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from typing import Any, Literal

Json = dict[str, Any]
Side = Literal["BUY", "SELL"]
SOURCE_REVISION = "34869838ed5588f6a4686f6663e824212287ddb3"
HTTP_URL = "https://perpdex-testnet.bond.xyz"
WS_URL = "wss://perpdex-testnet.bond.xyz/ws"
WS_API_URL = "wss://perpdex-testnet.bond.xyz/ws-fapi/v1"
CHAIN_ID = 16602
VIRTUAL_BOOKS = {
    "BTCUSDCPERP": (2, "0xd4d496823906464b0ae886e458cd46834a9c1640"),
    "ETHUSDCPERP": (4, "0x9bd7c857a10b1049e6dbc4061de36aa1e1c2d969"),
    "SOLUSDCPERP": (6, "0x8d2c48d2e3de9b8085fcc33105090eb0d0b9fd5a"),
    "0GUSDCPERP": (8, "0xcc4bbfeb0623f833eea937f6bbd24f4ab550f7d1"),
}


class SafetyError(ValueError):
    """A fail-closed guard blocked an unsupported or unsafe operation."""


class UnknownOutcome(RuntimeError):
    """A command may have taken effect; do not retry or replace it automatically."""


def decimal(value: str) -> Decimal:
    if not isinstance(value, str) or not value or len(value) > 80:
        raise SafetyError("Expected a bounded decimal string")
    result = Decimal(value)
    if not result.is_finite() or abs(result) > Decimal("1e20"):
        raise SafetyError("Non-finite or excessive decimal")
    return result


def units(value: Decimal, precision: int) -> int:
    with localcontext() as ctx:
        ctx.prec = 80
        scaled = value * 10**precision
        if not scaled.is_finite() or scaled != scaled.to_integral_value():
            raise SafetyError("Value exceeds protocol precision")
        return int(scaled)


def text(value: Decimal) -> str:
    fixed = format(value, "f")
    return fixed.rstrip("0").rstrip(".") if "." in fixed else fixed


@dataclass(frozen=True)
class TestnetConfig:
    chain_id: int = CHAIN_ID
    http_url: str = HTTP_URL
    ws_url: str = WS_URL
    ws_api_url: str = WS_API_URL
    allow_live_private: bool = False
    allow_live_orders: bool = False
    max_order_notional: str = "200"
    max_position: str = "0.010"
    max_open_orders: int = 2

    def validate(self) -> None:
        if (self.chain_id, self.http_url, self.ws_url, self.ws_api_url) != (
            CHAIN_ID,
            HTTP_URL,
            WS_URL,
            WS_API_URL,
        ):
            raise SafetyError("Only the source-pinned testnet identity and exact URLs are allowed")
        if type(self.allow_live_private) is not bool or type(self.allow_live_orders) is not bool:
            raise SafetyError("Live permissions must be explicit booleans")
        if self.allow_live_orders and not self.allow_live_private:
            raise SafetyError("Live orders also require explicit private-session permission")
        if decimal(self.max_order_notional) <= 0 or decimal(self.max_position) <= 0:
            raise SafetyError("Risk limits must be positive decimal strings")
        if type(self.max_open_orders) is not int or not 1 <= self.max_open_orders <= 20:
            raise SafetyError("Open-order limit must be between 1 and 20")


@dataclass(frozen=True)
class Market:
    symbol: str
    product_id: int
    virtual_book: str
    tick: Decimal
    step: Decimal
    min_quantity: Decimal
    max_quantity: Decimal
    min_price: Decimal
    max_price: Decimal
    min_notional: Decimal
    base_precision: int = 8

    @classmethod
    def from_exchange_info(cls, response: Json, symbol: str) -> "Market":
        if symbol not in VIRTUAL_BOOKS:
            raise SafetyError("Market is not in the pinned testnet registry")
        matches = [item for item in response["symbols"] if item["symbol"] == symbol]
        if len(matches) != 1:
            raise SafetyError("Missing or ambiguous exchangeInfo market")
        item = matches[0]
        if item["status"] != "TRADING" or item["baseAssetPrecision"] != 8:
            raise SafetyError("Unsupported market status or precision")
        if "POST_ONLY" not in item["orderTypes"]:
            raise SafetyError("Market does not advertise POST_ONLY")
        filters = {entry["filterType"]: entry for entry in item["filters"]}
        price, lot, notional = (
            filters[key] for key in ("PRICE_FILTER", "LOT_SIZE", "MIN_NOTIONAL")
        )
        product_id, book = VIRTUAL_BOOKS[symbol]
        market = cls(
            symbol,
            product_id,
            book,
            decimal(price["tickSize"]),
            decimal(lot["stepSize"]),
            decimal(lot["minQty"]),
            decimal(lot["maxQty"]),
            decimal(price["minPrice"]),
            decimal(price["maxPrice"]),
            decimal(str(notional["notional"])),
        )
        if min(market.tick, market.step, market.min_quantity, market.min_notional) <= 0:
            raise SafetyError("Invalid market filters")
        return market

    def validate_order(self, price: Decimal, quantity: Decimal) -> None:
        if not price.is_finite() or not quantity.is_finite():
            raise SafetyError("Non-finite order")
        if not self.min_price <= price <= self.max_price or price % self.tick:
            raise SafetyError("Price outside PRICE_FILTER")
        if not self.min_quantity <= quantity <= self.max_quantity or quantity % self.step:
            raise SafetyError("Quantity outside LOT_SIZE")
        if price * quantity < self.min_notional:
            raise SafetyError("Order below MIN_NOTIONAL")
        units(price, 9)
        units(quantity, self.base_precision)


@dataclass(frozen=True)
class Quote:
    side: Side
    price: Decimal
    quantity: Decimal


@dataclass(frozen=True)
class PreparedRequest:
    method: str
    path: str
    body: str = field(repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class OrderIntent:
    client_order_id: str
    digest: str
    request: PreparedRequest = field(repr=False)


@dataclass(frozen=True)
class AccountSnapshot:
    symbol: str
    positions: list[Json]
    open_orders: list[Json]
    observed_at_ms: int


@dataclass(frozen=True)
class UserStreamMessage:
    kind: Literal["connected", "event", "disconnected"]
    generation: int
    subscription_id: int | None
    requires_reconciliation: bool
    reason: str
    event: Json | None = None
