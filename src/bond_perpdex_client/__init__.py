"""Example BondPerpDEX client with explicitly enabled testnet-only private APIs."""

from .chain import read_endpoint_time
from .client import BondPerpDexClient
from .client_orders import fair_price_band, marketable_limit_price
from .credentials import load_ed25519_private_key
from .models import (
    GALILEO_RPC_URL,
    AccountSnapshot,
    Market,
    OrderIntent,
    PriceBand,
    Quote,
    SafetyError,
    TestnetConfig,
    UnknownOutcome,
    UserStreamMessage,
)
from .registry import SigningRegistry
from .signing import WalletSigner, decode_expiration, encode_expiration
from .transport import TestnetReadOnlyTransport, TestnetTransport
from .wire import websocket_cancel_all, websocket_order_place

__all__ = [
    "BondPerpDexClient",
    "GALILEO_RPC_URL",
    "PriceBand",
    "decode_expiration",
    "encode_expiration",
    "fair_price_band",
    "marketable_limit_price",
    "read_endpoint_time",
    "SigningRegistry",
    "load_ed25519_private_key",
    "websocket_cancel_all",
    "Market",
    "OrderIntent",
    "Quote",
    "SafetyError",
    "TestnetConfig",
    "TestnetReadOnlyTransport",
    "UnknownOutcome",
    "WalletSigner",
    "AccountSnapshot",
    "TestnetTransport",
    "UserStreamMessage",
    "websocket_order_place",
]
