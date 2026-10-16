"""Documented quick-start settings. Read dotenv data without executing shell code."""

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import InvalidOperation
from pathlib import Path

from .credentials import _read_private
from .models import (
    CHAIN_ID,
    HTTP_URL,
    SUPPORTED_SYMBOLS,
    WS_API_URL,
    WS_URL,
    SafetyError,
    TestnetConfig,
    decimal,
)


@dataclass(frozen=True)
class Setting:
    name: str
    default: str
    group: str
    module: str
    description: str
    secret: bool = False


SETTINGS = (
    Setting(
        "BOND_MODE",
        "offline",
        "Runtime",
        "quickstart",
        "offline, public, private, stream or trade; trade also needs --allow-live-orders.",
    ),
    Setting(
        "BOND_SUBACCOUNT",
        "tradegods-z",
        "Account",
        "client_session / signing",
        "Wallet-scoped name (1-12 ASCII bytes, no spaces); separate funding from website bond.",
    ),
    Setting(
        "BOND_TESTNET_WALLET_KEY",
        "",
        "Account",
        "signing",
        "Owner wallet secret; inject via a secret manager. Required for private modes.",
        True,
    ),
    Setting(
        "BOND_ED25519_PRIVATE_KEY_FILE",
        ".local/galileo-ed25519.pem",
        "Authentication",
        "credentials",
        "Persistent owner-only Ed25519 PKCS#8 PEM. setup --generate-signing-key can create it.",
    ),
    Setting(
        "BOND_SESSION_FILE",
        ".local/galileo.session.json",
        "Authentication",
        "client_session",
        "Owner-only token cache. Reuse a matching session; explicitly renew at 24 hours remaining.",
    ),
    Setting(
        "BOND_CHAIN_ID",
        str(CHAIN_ID),
        "Venue",
        "models / transport",
        "Galileo identity check; this example accepts only 16602.",
    ),
    Setting(
        "BOND_HTTP_URL",
        HTTP_URL,
        "Venue",
        "transport",
        "Pinned Galileo REST origin; alternate origins are rejected.",
    ),
    Setting(
        "BOND_PUBLIC_WS_URL",
        WS_URL,
        "Venue",
        "transport",
        "Pinned public market-data WebSocket; alternate URLs are rejected.",
    ),
    Setting(
        "BOND_TRADING_WS_URL",
        WS_API_URL,
        "Venue",
        "transport",
        "Pinned trading/private-event WebSocket; alternate URLs are rejected.",
    ),
    Setting(
        "BOND_REGISTRY_FILE",
        "perpdex.local.json",
        "Signing context",
        "registry",
        "Verified Galileo manifest for trade: product IDs, VirtualBooks and Endpoint.",
    ),
    Setting(
        "BOND_GALILEO_RPC_URL",
        "",
        "Signing context",
        "chain",
        "Verified HTTPS Galileo RPC for chain identity and Endpoint time; required for trade.",
        True,
    ),
    Setting(
        "BOND_SYMBOL",
        "BTCUSDCPERP",
        "Strategy",
        "models / strategy",
        "BTCUSDCPERP offline; BTCUSDCPERP, ETHUSDCPERP, SOLUSDCPERP or 0GUSDCPERP live.",
    ),
    Setting(
        "BOND_QUANTITY",
        "0.002",
        "Strategy",
        "strategy / client_orders",
        "Base size per quote/order; subject to venue lot and min-notional filters.",
    ),
    Setting(
        "BOND_HALF_SPREAD_BPS",
        "10",
        "Strategy",
        "strategy",
        "Offline maker half-spread in basis points, 1-1000. Not used to choose a live order price.",
    ),
    Setting(
        "BOND_ORDER_SIDE",
        "BUY",
        "Supervised order",
        "client_orders",
        "BUY or SELL for trade mode only.",
    ),
    Setting(
        "BOND_LIMIT_PRICE",
        "",
        "Supervised order",
        "client_orders",
        "Operator-chosen live limit price, required for trade; no example price is used.",
    ),
    Setting(
        "BOND_MAX_POSITION",
        "0.010",
        "Risk",
        "strategy / client_orders",
        "Maximum absolute per-market base position, including worst-case open-order fills.",
    ),
    Setting(
        "BOND_MAX_ORDER_NOTIONAL",
        "200",
        "Risk",
        "strategy / client_orders",
        "Maximum quote-currency notional per order.",
    ),
    Setting(
        "BOND_MAX_OPEN_ORDERS",
        "2",
        "Risk",
        "client_orders",
        "Live per-market open-order cap, 1-20; includes this account's other processes.",
    ),
    Setting(
        "BOND_MAX_FEED_AGE_MS",
        "2000",
        "Risk",
        "strategy",
        "Offline book/inventory freshness bound in milliseconds, 1-10000.",
    ),
    Setting(
        "BOND_MAX_EVENTS",
        "5",
        "Private stream",
        "user_stream",
        "Maximum delivered private events, 1-10000.",
    ),
    Setting(
        "BOND_MAX_RECONNECTS",
        "0",
        "Private stream",
        "user_stream",
        "Bounded private-stream reconnect budget, 0-3; never replays orders or renews SIWE.",
    ),
    Setting(
        "BOND_STREAM_IDLE_TIMEOUT_SECONDS",
        "5",
        "Private stream",
        "user_stream",
        "Event idle wait in seconds, 1-60; a quiet stream ends with REST reconciliation.",
    ),
)
BY_NAME = {setting.name: setting for setting in SETTINGS}
MODES = ("offline", "public", "private", "stream", "trade")


def template(overrides: Mapping[str, str] | None = None) -> str:
    overrides = overrides or {}
    lines = [
        "# Generated by python -m bond_perpdex_client.setup --print-template",
        "# Edit values as data; this file is never sourced or shell-expanded.",
        "# Process environment overrides this file. Keep .env owner-only and untracked.",
    ]
    group = None
    for setting in SETTINGS:
        if group != setting.group:
            lines += ["", f"# {setting.group}"]
            group = setting.group
        value = overrides.get(setting.name, setting.default)
        if value.startswith(("'", '"')):
            value = f'"{value}"'
        lines += [
            f"# {setting.description}",
            f"{setting.name}={value}",
        ]
    return "\n".join(lines) + "\n"


def reference_table() -> str:
    lines = ["| Variable | Default | Layer / module | Effect |", "| --- | --- | --- | --- |"]
    for setting in SETTINGS:
        default = f"`{setting.default}`" if setting.default else "Empty; maker supplied"
        lines.append(
            f"| `{setting.name}` | {default} | {setting.group}: `{setting.module}` | "
            f"{setting.description} |"
        )
    return "\n".join(lines) + "\n"


def parse_dotenv(content: str) -> dict[str, str]:
    values = {}
    for line_number, line in enumerate(content.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or key not in BY_NAME or key in values:
            raise SafetyError(
                f"Unknown, duplicate or malformed setting on dotenv line {line_number}"
            )
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise SafetyError(f"Unclosed quoted setting on dotenv line {line_number}")
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


@dataclass(repr=False)
class Settings:
    values: dict[str, str] = field(repr=False)
    root: Path

    @classmethod
    def load(cls, env_file: str | Path, environment: Mapping[str, str] | None = None) -> "Settings":
        path = Path(env_file).absolute()
        values = {setting.name: setting.default for setting in SETTINGS}
        values.update(parse_dotenv(_read_private(path).decode()))
        environment = os.environ if environment is None else environment
        values.update({name: environment[name] for name in BY_NAME if name in environment})
        return cls(values, path.parent)

    @property
    def mode(self) -> str:
        return self.values["BOND_MODE"]

    def integer(self, name: str, minimum: int, maximum: int) -> int:
        try:
            value = int(self.values[name])
            if not minimum <= value <= maximum:
                raise ValueError
            return value
        except (TypeError, ValueError):
            raise SafetyError(f"{name} must be an integer from {minimum} to {maximum}") from None

    def positive(self, name: str):
        try:
            value = decimal(self.values[name])
            if value <= 0:
                raise ValueError
            return value
        except (ValueError, InvalidOperation):
            raise SafetyError(f"{name} must be a positive decimal") from None

    def path(self, name: str) -> Path:
        value = self.values[name]
        if not value:
            raise SafetyError(f"{name} requires a file path")
        path = Path(value).expanduser()
        return path if path.is_absolute() else self.root / path

    def client_config(self, *, allow_orders: bool = False, registry=None) -> TestnetConfig:
        config = TestnetConfig(
            chain_id=self.integer("BOND_CHAIN_ID", CHAIN_ID, CHAIN_ID),
            http_url=self.values["BOND_HTTP_URL"],
            ws_url=self.values["BOND_PUBLIC_WS_URL"],
            ws_api_url=self.values["BOND_TRADING_WS_URL"],
            allow_live_private=self.mode in {"private", "stream", "trade"},
            allow_live_orders=allow_orders,
            signing_registry=registry,
            max_position=self.values["BOND_MAX_POSITION"],
            max_order_notional=self.values["BOND_MAX_ORDER_NOTIONAL"],
            max_open_orders=self.integer("BOND_MAX_OPEN_ORDERS", 1, 20),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.mode not in MODES:
            raise SafetyError("BOND_MODE must be offline, public, private, stream or trade")
        if not re.fullmatch(r"[\x21-\x7e]{1,12}", self.values["BOND_SUBACCOUNT"]):
            raise SafetyError("BOND_SUBACCOUNT must be 1-12 printable ASCII bytes without spaces")
        if self.values["BOND_SYMBOL"] not in SUPPORTED_SYMBOLS:
            raise SafetyError("BOND_SYMBOL must select a supported Galileo market")
        if self.mode == "offline" and self.values["BOND_SYMBOL"] != "BTCUSDCPERP":
            raise SafetyError(
                "The offline fixture supports only BTCUSDCPERP; live modes use live markets"
            )
        for name in ("BOND_QUANTITY", "BOND_MAX_POSITION", "BOND_MAX_ORDER_NOTIONAL"):
            self.positive(name)
        self.integer("BOND_HALF_SPREAD_BPS", 1, 1000)
        self.integer("BOND_MAX_FEED_AGE_MS", 1, 10_000)
        self.integer("BOND_MAX_EVENTS", 1, 10_000)
        self.integer("BOND_MAX_RECONNECTS", 0, 3)
        self.integer("BOND_STREAM_IDLE_TIMEOUT_SECONDS", 1, 60)
        if self.values["BOND_ORDER_SIDE"] not in {"BUY", "SELL"}:
            raise SafetyError("BOND_ORDER_SIDE must be BUY or SELL")
        if self.values["BOND_LIMIT_PRICE"]:
            self.positive("BOND_LIMIT_PRICE")
        self.client_config()

    def legacy_settings(self) -> dict:
        """Feed the existing public/offline demonstration, preserving its permissions."""
        return {
            "mode": "offline",
            "chain_id": CHAIN_ID,
            "http_url": self.values["BOND_HTTP_URL"],
            "ws_url": self.values["BOND_PUBLIC_WS_URL"],
            "ws_api_url": self.values["BOND_TRADING_WS_URL"],
            "symbol": self.values["BOND_SYMBOL"],
            "subaccount": self.values["BOND_SUBACCOUNT"],
            "quantity": self.values["BOND_QUANTITY"],
            "half_spread_bps": self.integer("BOND_HALF_SPREAD_BPS", 1, 1000),
            "max_position": self.values["BOND_MAX_POSITION"],
            "max_order_notional": self.values["BOND_MAX_ORDER_NOTIONAL"],
            "max_open_orders": self.integer("BOND_MAX_OPEN_ORDERS", 1, 20),
            "max_feed_age_ms": self.integer("BOND_MAX_FEED_AGE_MS", 1, 10_000),
        }

    def redacted_json(self) -> str:
        return json.dumps(
            {
                setting.name: ("<set>" if self.values[setting.name] else "<unset>")
                if setting.secret
                else self.values[setting.name]
                for setting in SETTINGS
            },
            indent=2,
        )
