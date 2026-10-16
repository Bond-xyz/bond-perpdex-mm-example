"""Validate public Galileo signing discovery and optional operator-supplied pins."""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Ambiguous duplicate registry field")
        result[key] = value
    return result


def address(value: object) -> str:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"0x[0-9a-fA-F]{40}", value)
        or int(value, 16) == 0
    ):
        raise ValueError("Expected a nonzero EVM address in the verified registry")
    return value.lower()


@dataclass(frozen=True)
class SigningRegistry:
    markets: Mapping[str, tuple[int, str]]
    endpoint: str
    sha256: str

    @classmethod
    def from_exchange_info(cls, data: dict) -> "SigningRegistry":
        """Read the public extension; never guess addresses from symbols or fixtures."""
        symbols = data.get("symbols")
        if not isinstance(symbols, list) or not symbols:
            raise ValueError("exchangeInfo signing discovery requires a nonempty symbols array")
        markets = {}
        for item in symbols:
            if not isinstance(item, dict) or not isinstance(item.get("symbol"), str):
                raise ValueError("Malformed exchangeInfo signing market")
            symbol = item["symbol"]
            if symbol in markets:
                raise ValueError("Duplicate exchangeInfo signing symbol")
            if "productId" not in item or "virtualBook" not in item:
                raise ValueError(
                    "exchangeInfo lacks productId/virtualBook; signing discovery unavailable"
                )
            markets[symbol] = {"productId": item["productId"], "virtualBook": item["virtualBook"]}
        public = {
            "chainId": data.get("chainId"),
            "contracts": {"endpointProxy": data.get("endpoint")},
            "markets": markets,
        }
        digest = hashlib.sha256(json.dumps(public, sort_keys=True).encode()).hexdigest()
        return cls.from_dict(public, digest)

    @classmethod
    def from_file(cls, path: str | Path) -> "SigningRegistry":
        raw = Path(path).read_bytes()
        if len(raw) > 1_000_000:
            raise ValueError("Registry exceeds size bound")
        return cls.from_dict(
            json.loads(raw, object_pairs_hook=_unique_fields), hashlib.sha256(raw).hexdigest()
        )

    @classmethod
    def from_dict(cls, data: dict, sha256: str = "synthetic-offline-fixture") -> "SigningRegistry":
        if type(data.get("chainId")) is not int or data["chainId"] != 16602:
            raise ValueError("Signing registry must select Galileo chain 16602")
        markets = {}
        for symbol, entry in data["markets"].items():
            product = entry["productId"]
            if (
                type(product) is not int
                or not 0 < product < 2**32
                or not re.fullmatch(r"[A-Z0-9]+PERP", symbol)
            ):
                raise ValueError("Invalid registry market or product ID")
            markets[symbol] = (product, address(entry["virtualBook"]))
        if not markets or len({p for p, _ in markets.values()}) != len(markets):
            raise ValueError("Registry markets must have distinct product IDs")
        if len({book for _, book in markets.values()}) != len(markets):
            raise ValueError("Registry markets must have distinct VirtualBooks")
        return cls(MappingProxyType(markets), address(data["contracts"]["endpointProxy"]), sha256)
