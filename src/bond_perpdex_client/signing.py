"""The SIWE, Ed25519 form, and Vertex EIP-712 schemes used by the source client."""

import base64
import json
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from urllib.parse import quote_plus

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from eth_account import Account
from eth_account.messages import SignableMessage, encode_defunct, encode_typed_data
from eth_utils import keccak

from .models import CHAIN_ID, Json, Market, SafetyError, Side, units

ORDER_TYPE = "Order(bytes32 sender,int128 priceX18,int128 amount,uint64 expiration,uint64 nonce)"
ORDER_FIELDS = [
    {"name": "sender", "type": "bytes32"},
    {"name": "priceX18", "type": "int128"},
    {"name": "amount", "type": "int128"},
    {"name": "expiration", "type": "uint64"},
    {"name": "nonce", "type": "uint64"},
]

OrderType = Literal["LIMIT", "POST_ONLY"]
TimeInForce = Literal["GTC", "IOC", "FOK"]
# Signed ``expiration`` (uint64): bits 0-57 expiry Unix seconds, bits 58-60 reserved
# (zero), bit 61 reduce-only, bits 62-63 order mode.
EXPIRY_MASK = (1 << 58) - 1
RESERVED_MASK = 0b111 << 58
REDUCE_ONLY_BIT = 1 << 61
ORDER_MODES: dict[tuple[str, str], int] = {
    ("LIMIT", "GTC"): 0,
    ("LIMIT", "IOC"): 1,
    ("LIMIT", "FOK"): 2,
    ("POST_ONLY", "GTC"): 3,
}


def encode_expiration(
    order_type: OrderType, time_in_force: TimeInForce, reduce_only: bool, expires_at: int
) -> int:
    """Pack the signed order mode, reduce-only flag and expiry seconds into one uint64.

    The request ``type``, ``timeInForce`` and ``reduceOnly`` fields must equal these
    signed bits. Signed MARKET orders are unsupported: send LIMIT/IOC at the worst
    acceptable price instead.
    """
    mode = ORDER_MODES.get((order_type, time_in_force))
    if mode is None:
        raise SafetyError(
            "Signed orders support LIMIT/GTC, LIMIT/IOC, LIMIT/FOK and POST_ONLY/GTC; "
            "use LIMIT/IOC at the worst acceptable price instead of MARKET"
        )
    if type(reduce_only) is not bool:
        raise SafetyError("reduce_only must be a boolean")
    if reduce_only and time_in_force not in {"IOC", "FOK"}:
        raise SafetyError("Reduce-only is valid only with IOC or FOK")
    if type(expires_at) is not int or not 0 < expires_at <= EXPIRY_MASK:
        raise SafetyError("Expiration must fit the low 58 timestamp bits")
    return (mode << 62) | (REDUCE_ONLY_BIT if reduce_only else 0) | expires_at


def decode_expiration(expiration: int) -> tuple[OrderType, TimeInForce, bool, int]:
    """Return ``(order_type, time_in_force, reduce_only, expires_at)`` or fail closed."""
    if type(expiration) is not int or not 0 <= expiration < 2**64:
        raise SafetyError("Signed expiration must be a uint64")
    if expiration & RESERVED_MASK:
        raise SafetyError("Signed expiration reserved bits 58-60 must be zero")
    mode = expiration >> 62
    order_type, time_in_force = next(key for key, value in ORDER_MODES.items() if value == mode)
    reduce_only = bool(expiration & REDUCE_ONLY_BIT)
    expires_at = expiration & EXPIRY_MASK
    # Re-encode to apply the same validity rules (reduce-only mode, nonzero expiry).
    encode_expiration(order_type, time_in_force, reduce_only, expires_at)
    return order_type, time_in_force, reduce_only, expires_at


def compact(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def canonical_form(fields: Json) -> str:
    def encode(value: object) -> str:
        raw = value if isinstance(value, str) else compact(value)
        return quote_plus(raw, safe="*").replace("~", "%7E")

    return "&".join(
        f"{encode(key)}={encode(value)}"
        for key, value in sorted(fields.items())
        if value is not None and key not in {"signature", "apiKey"}
    )


def signed_form(fields: Json, key: Ed25519PrivateKey) -> str:
    body = canonical_form(fields)
    signature = base64.b64encode(key.sign(body.encode())).decode()
    return body + "&signature=" + quote_plus(signature, safe="")


def subaccount_sender(owner: str, name: str) -> str:
    encoded = name.encode()
    if not 1 <= len(encoded) <= 12 or any(byte < 33 or byte > 126 for byte in encoded):
        raise SafetyError("Use a 1-12 byte printable ASCII subaccount name without spaces")
    return owner.lower() + encoded.hex().ljust(24, "0")


def typed_message(domain: Json, order: Json) -> SignableMessage:
    return encode_typed_data(domain, {"Order": ORDER_FIELDS}, order)


def order_hashes(domain: Json, order: Json) -> Json:
    message = typed_message(domain, order)
    return {
        "typeHash": "0x" + keccak(text=ORDER_TYPE).hex(),
        "domainSeparator": "0x" + message.header.hex(),
        "structHash": "0x" + message.body.hex(),
        "digest": "0x" + keccak(b"\x19\x01" + message.header + message.body).hex(),
    }


class WalletSigner:
    def __init__(self, private_key: bytes) -> None:
        self._account = Account.from_key(private_key)

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> "WalletSigner":
        values = os.environ if environment is None else environment
        raw = values.get("BOND_TESTNET_WALLET_KEY", "")
        if not re.fullmatch(r"(?:0x)?[0-9a-fA-F]{64}", raw):
            raise SafetyError("BOND_TESTNET_WALLET_KEY must contain a 32-byte testnet wallet key")
        try:
            return cls(bytes.fromhex(raw.removeprefix("0x")))
        except Exception:
            raise SafetyError("Invalid testnet wallet key") from None

    @property
    def address(self) -> str:
        return self._account.address

    def signin(self, nonce: str, key: Ed25519PrivateKey, subaccount: str, now_ms: int) -> Json:
        subaccount_sender(self.address, subaccount)
        if not nonce.isalnum() or len(nonce) < 8:
            raise SafetyError("Invalid SIWE nonce")
        public = key.public_key()
        pem = public.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode()
        raw = public.public_bytes_raw().hex()
        issued = datetime.fromtimestamp(now_ms / 1000, UTC).isoformat()
        message = (
            f"bond.xyz wants you to sign in with your Ethereum account:\n{self.address}\n\n"
            f"Sign in to Bond Perp. I'll sign my future operations with {raw} using ED25519.\n\n"
            f"URI: https://bond.xyz\nVersion: 1\nChain ID: {CHAIN_ID}\nNonce: {nonce}\n"
            f"Issued At: {issued}\nRequest ID: {subaccount}"
        )
        signature = self._account.sign_message(encode_defunct(text=message)).signature
        return {
            "message": message,
            "signature": "0x" + signature.hex(),
            "secret_type": "Ed25519",
            "secret_key": pem,
        }

    def order(
        self,
        market: Market,
        side: Side,
        price: Decimal,
        quantity: Decimal,
        subaccount: str,
        expires_at: int,
        nonce: int,
        *,
        order_type: OrderType = "POST_ONLY",
        time_in_force: TimeInForce = "GTC",
        reduce_only: bool = False,
    ) -> Json:
        market.validate_order(price, quantity)
        if side not in {"BUY", "SELL"} or type(nonce) is not int or not 0 < nonce < 2**64:
            raise SafetyError("Invalid side or uint64 order nonce")
        expiration = encode_expiration(order_type, time_in_force, reduce_only, expires_at)
        domain = {
            "name": "Vertex",
            "version": "0.0.1",
            "chainId": CHAIN_ID,
            "verifyingContract": market.virtual_book,
        }
        amount = units(quantity, market.base_precision) * 10 ** (18 - market.base_precision)
        order = {
            "sender": subaccount_sender(self.address, subaccount),
            "priceX18": str(units(price, 9) * 10**9),
            "amount": str(amount if side == "BUY" else -amount),
            "expiration": str(expiration),
            "nonce": str(nonce),
        }
        signature = self._account.sign_message(typed_message(domain, order)).signature
        return {
            "schemaVersion": 1,
            "productId": market.product_id,
            "domain": domain,
            "order": order,
            "signer": self.address.lower(),
            "signature": "0x" + signature.hex(),
            "hashes": order_hashes(domain, order),
        }
