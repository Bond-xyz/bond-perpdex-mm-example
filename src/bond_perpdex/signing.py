"""The SIWE, Ed25519 form, and Vertex EIP-712 schemes used by the source client."""

import base64
import json
import os
import re
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import quote_plus

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from eth_account import Account
from eth_account.messages import SignableMessage, encode_defunct, encode_typed_data
from eth_utils import keccak

from .models import CHAIN_ID, VIRTUAL_BOOKS, Json, Market, SafetyError, Side, units

ORDER_TYPE = "Order(bytes32 sender,int128 priceX18,int128 amount,uint64 expiration,uint64 nonce)"
ORDER_FIELDS = [
    {"name": "sender", "type": "bytes32"},
    {"name": "priceX18", "type": "int128"},
    {"name": "amount", "type": "int128"},
    {"name": "expiration", "type": "uint64"},
    {"name": "nonce", "type": "uint64"},
]


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
    if not 1 <= len(encoded) <= 12 or not name.isascii() or not name.isalnum():
        raise SafetyError("Use a 1-12 byte ASCII alphanumeric subaccount name")
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
    def from_environment(cls) -> "WalletSigner":
        raw = os.environ.get("BOND_TESTNET_WALLET_KEY", "")
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
    ) -> Json:
        if (market.product_id, market.virtual_book) != VIRTUAL_BOOKS.get(market.symbol):
            raise SafetyError("Order domain differs from the pinned testnet registry")
        market.validate_order(price, quantity)
        if side not in {"BUY", "SELL"} or type(nonce) is not int or not 0 < nonce < 2**64:
            raise SafetyError("Invalid side or uint64 order nonce")
        if type(expires_at) is not int or not 0 < expires_at < 2**58:
            raise SafetyError("Expiration must fit the low 58 timestamp bits")
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
            "expiration": str(expires_at | (3 << 62)),
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
