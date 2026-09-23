"""In-memory protocol-shaped fixture venue. It never opens a socket or models matching."""

import base64
import copy
import hashlib
import json
import uuid
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from eth_account import Account
from eth_account.messages import encode_defunct

from .models import Json, Market, PreparedRequest, SafetyError, decimal, units
from .signing import WalletSigner, canonical_form, order_hashes, subaccount_sender, typed_message

NOW_MS = 1_770_000_000_000
EXCHANGE_INFO = {
    "timezone": "UTC",
    "serverTime": NOW_MS,
    "symbols": [
        {
            "symbol": "BTCUSDCPERP",
            "status": "TRADING",
            "baseAssetPrecision": 8,
            "quotePrecision": 6,
            "orderTypes": ["LIMIT", "MARKET", "POST_ONLY"],
            "filters": [
                {
                    "filterType": "PRICE_FILTER",
                    "minPrice": "556.80",
                    "maxPrice": "4529764",
                    "tickSize": "0.10",
                },
                {
                    "filterType": "LOT_SIZE",
                    "minQty": "0.001",
                    "maxQty": "1000",
                    "stepSize": "0.001",
                },
                {"filterType": "MIN_NOTIONAL", "notional": 100},
            ],
        }
    ],
}
SNAPSHOT = {"lastUpdateId": 10, "bids": [["65000", "0.1"]], "asks": [["65010", "0.1"]]}
DEPTH_FRAME = {
    "stream": "btcusdcperp@depth@100ms",
    "data": {
        "e": "depthUpdate",
        "E": NOW_MS,
        "s": "BTCUSDCPERP",
        "U": 11,
        "u": 11,
        "pu": 10,
        "b": [["65000", "0.2"]],
        "a": [],
    },
}


def demo_identity() -> tuple[WalletSigner, Ed25519PrivateKey]:
    """Public deterministic simulation identity, never a funded credential."""
    wallet = WalletSigner(hashlib.sha256(b"BondPerpDex offline demo NOT FOR FUNDS wallet").digest())
    key = Ed25519PrivateKey.from_private_bytes(
        hashlib.sha256(b"BondPerpDex offline demo NOT FOR FUNDS session").digest()
    )
    return wallet, key


class OfflineVenue:
    offline = True

    def __init__(self):
        self.now_ms = NOW_MS
        self.orders: dict[str, Json] = {}
        self.calls: list[tuple[str, str]] = []
        self.nonces: set[str] = set()
        self.owner: str | None = None
        self.public_key = None
        self.subaccount = "bond"
        self.fail_after_accept = False
        self.fail_cancel = False
        self._nonce_issued = False

    def request(self, request: PreparedRequest) -> object:
        parsed = urlsplit(request.path)
        self.calls.append((request.method, parsed.path))
        if request.method == "GET" and parsed.path == "/fapi/v1/exchangeInfo":
            return copy.deepcopy(EXCHANGE_INFO)
        if request.method == "GET" and parsed.path == "/fapi/v1/depth":
            if parse_qs(parsed.query) != {"limit": ["20"], "symbol": ["BTCUSDCPERP"]}:
                raise SafetyError("Offline depth fixture supports only BTCUSDCPERP, limit 20")
            return copy.deepcopy(SNAPSHOT)
        if request.method == "GET" and parsed.path == "/auth/nonce":
            self._nonce_issued = True
            return {"nonce": "offlineNonce0001"}
        if request.method == "POST" and parsed.path == "/auth/signin":
            body = json.loads(request.body)
            message = body["message"]
            if not self._nonce_issued or "\nNonce: offlineNonce0001\n" not in message:
                raise SafetyError("Missing or replayed SIWE nonce")
            self._nonce_issued = False
            if body["secret_type"] != "Ed25519" or "\nChain ID: 16602\n" not in message:
                raise SafetyError("Wrong SIWE chain or key type")
            owner = Account.recover_message(
                encode_defunct(text=message), signature=body["signature"]
            )
            if owner != message.splitlines()[1]:
                raise SafetyError("SIWE signer mismatch")
            public = serialization.load_pem_public_key(body["secret_key"].encode())
            if public.public_bytes_raw().hex() not in message:
                raise SafetyError("SIWE statement must bind the session key")
            self.owner = owner
            self.public_key = public
            self.subaccount = message.split("\nRequest ID: ")[1]
            return {
                "api_key": "offline-session-not-a-credential",
                "expires_at": "2026-02-02T03:40:00+00:00",
                "id": "00000000-0000-4000-8000-000000000001",
            }
        fields = self._verify_request(request)
        if request.method == "GET" and fields.get("symbol") != "BTCUSDCPERP":
            raise SafetyError("Offline account fixture supports only BTCUSDCPERP")
        if request.method == "GET" and parsed.path == "/fapi/v1/positionRisk":
            return [{"symbol": "BTCUSDCPERP", "positionAmt": "0", "updateTime": self.now_ms}]
        if request.method == "GET" and parsed.path == "/fapi/v1/openOrders":
            return [
                copy.deepcopy(order) for order in self.orders.values() if order["status"] == "NEW"
            ]
        if request.method == "GET" and parsed.path == "/fapi/v1/order":
            return next(
                copy.deepcopy(order)
                for order in self.orders.values()
                if order["clientOrderId"] == fields.get("origClientOrderId")
                or order["orderId"] == fields.get("orderId")
            )
        if request.method == "POST" and parsed.path == "/fapi/v1/order":
            return self._place(fields)
        if request.method == "DELETE" and parsed.path == "/fapi/v1/order":
            if fields["productId"] != "2":
                raise SafetyError("Wrong cancellation product")
            order = self.orders[fields["orderId"]]
            order["status"] = "CANCELED"
            if self.fail_cancel:
                raise TimeoutError("Simulated lost cancellation acknowledgement")
            acknowledgement = copy.deepcopy(order)
            acknowledgement.pop("status")
            acknowledgement.update(
                {"clientOrderId": fields["newClientOrderId"], "productId": 2, "type": "LIMIT"}
            )
            return acknowledgement
        if request.method == "DELETE" and parsed.path == "/fapi/v1/openOrders":
            if fields["symbol"] != "BTCUSDCPERP" or not fields.get("newClientOrderId"):
                raise SafetyError("Invalid cancel-all symbol or command receipt ID")
            cancelled = []
            for order in self.orders.values():
                if order["status"] == "NEW":
                    order["status"] = "CANCELED"
                    acknowledgement = copy.deepcopy(order)
                    acknowledgement.pop("clientOrderId")
                    acknowledgement.update(
                        {"origClientOrderId": order["orderId"], "productId": 2, "type": "LIMIT"}
                    )
                    cancelled.append(acknowledgement)
            if self.fail_cancel:
                raise TimeoutError("Simulated lost cancel-all acknowledgement")
            return cancelled
        raise SafetyError("Unsupported offline route")

    def _verify_request(self, request: PreparedRequest) -> Json:
        if (
            request.headers.get("x-api-key") != "offline-session-not-a-credential"
            or self.public_key is None
        ):
            raise SafetyError("Missing session")
        encoded = urlsplit(request.path).query if request.method == "GET" else request.body
        pairs = parse_qs(encoded, keep_blank_values=True, strict_parsing=True)
        if any(len(values) != 1 for values in pairs.values()):
            raise SafetyError("Duplicate transport fields")
        fields = {name: values[0] for name, values in pairs.items()}
        signature = base64.b64decode(fields.pop("signature"), validate=True)
        self.public_key.verify(signature, canonical_form(fields).encode())
        if not 0 <= self.now_ms - int(fields["timestamp"]) <= int(fields["recvWindow"]):
            raise SafetyError("Expired request")
        return fields

    def _place(self, fields: Json) -> Json:
        market = Market.from_exchange_info(EXCHANGE_INFO, fields["symbol"])
        price, quantity = decimal(fields["price"]), decimal(fields["quantity"])
        market.validate_order(price, quantity)
        envelope = json.loads(fields["signedOrder"])
        order, domain = envelope["order"], envelope["domain"]
        if domain != {
            "name": "Vertex",
            "version": "0.0.1",
            "chainId": 16602,
            "verifyingContract": market.virtual_book,
        }:
            raise SafetyError("Wrong EIP-712 domain")
        if envelope["schemaVersion"] != 1 or envelope["productId"] != market.product_id:
            raise SafetyError("Wrong envelope schema or product")
        if fields["type"] != "POST_ONLY" or fields["timeInForce"] != "GTC":
            raise SafetyError("Only POST_ONLY/GTC in this fixture")
        expiration = int(order["expiration"])
        if expiration >> 58 != 48 or (expiration & (2**58 - 1)) <= self.now_ms // 1000:
            raise SafetyError("Wrong expiration flags or expired order")
        if order["nonce"] in self.nonces:
            raise SafetyError("Reused signed order nonce")
        if order["sender"] != subaccount_sender(self.owner, self.subaccount):
            raise SafetyError("Wrong authenticated subaccount")
        if int(order["priceX18"]) != units(price, 18):
            raise SafetyError("Signed price differs from transport")
        expected_amount = units(quantity, 18) * (1 if fields["side"] == "BUY" else -1)
        if fields["side"] not in {"BUY", "SELL"} or int(order["amount"]) != expected_amount:
            raise SafetyError("Signed amount differs from transport")
        signer = Account.recover_message(
            typed_message(domain, order), signature=envelope["signature"]
        )
        if signer != self.owner or signer.lower() != envelope["signer"]:
            raise SafetyError("Invalid wallet signature")
        if envelope["hashes"] != order_hashes(domain, order):
            raise SafetyError("Invalid EIP-712 hash evidence")
        self.nonces.add(order["nonce"])
        order_id = str(uuid.uuid5(uuid.NAMESPACE_OID, fields["newClientOrderId"]))
        result = {
            "symbol": fields["symbol"],
            "orderId": order_id,
            "clientOrderId": fields["newClientOrderId"],
            "price": fields["price"],
            "origQty": fields["quantity"],
            "executedQty": "0",
            "status": "NEW",
            "side": fields["side"],
            "type": "POST_ONLY",
            "timeInForce": "GTC",
            "transactTime": self.now_ms,
        }
        self.orders[order_id] = result
        if self.fail_after_accept:
            raise TimeoutError("Simulated lost acknowledgement after acceptance")
        return copy.deepcopy(result)
