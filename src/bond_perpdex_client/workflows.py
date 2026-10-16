"""Shared credential setup and supervised flows for the example entry points."""

import argparse
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .chain import read_endpoint_time
from .client import BondPerpDexClient
from .credentials import load_ed25519_private_key
from .models import Json, Quote, SafetyError, TestnetConfig
from .registry import SigningRegistry
from .signing import WalletSigner
from .transport import TestnetTransport


@dataclass(frozen=True, repr=False)
class ConnectedAccount:
    client: BondPerpDexClient
    wallet: WalletSigner
    subaccount: str


def add_credential_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--subaccount", required=True)
    parser.add_argument(
        "--ed25519-key", type=Path, required=True, help="Owner-only persistent Ed25519 PKCS#8 PEM"
    )
    parser.add_argument(
        "--session-file",
        type=Path,
        help="Owner-only session cache; renew within 24 hours of expiry",
    )


def open_account(
    config: TestnetConfig,
    *,
    subaccount: str,
    key_file: Path,
    session_file: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> ConnectedAccount:
    wallet = WalletSigner.from_environment(environment)
    key = load_ed25519_private_key(key_file)
    client = BondPerpDexClient(TestnetTransport(config))
    if config.allow_live_orders:
        client.discover_signing_context()
    if session_file is None:
        client.authenticate(wallet, key, subaccount)
    else:
        client.authenticate_cached(wallet, key, subaccount, session_file)
    return ConnectedAccount(client, wallet, subaccount)


def print_identity(client: BondPerpDexClient) -> None:
    identity = client.identity()
    print(f"Backend subaccount UUID: {identity['id']}; encoded name: {identity['name']}")


def watch_account(
    client: BondPerpDexClient,
    symbol: str,
    *,
    max_events: int = 5,
    max_reconnects: int = 0,
    idle_timeout: float = 5,
) -> None:
    with closing(
        client.user_events(
            max_events=max_events, max_reconnects=max_reconnects, idle_timeout=idle_timeout
        )
    ) as messages:
        for message in messages:
            if message.kind == "connected":
                print(f"Subscription acknowledged: {message.subscription_id}")
            elif message.kind == "event":
                event = message.event or {}
                print(f"Event {event.get('e')}: {event.get('i', event.get('eventId', ''))}")
            else:
                print(f"Stream disconnected: {message.reason}")
            if message.requires_reconciliation:
                client.reconcile_account(symbol)
            if message.kind == "disconnected":
                print("REST account reconciliation completed")
                if message.generation >= max_reconnects:
                    break


def place_and_cancel(
    account: ConnectedAccount,
    symbol: str,
    quote: Quote,
    *,
    rpc_url: str,
    registry: SigningRegistry | None = None,
) -> Json:
    """Submit once, cancel once, then require a terminal projection; never replay."""
    client = account.client
    market = client.market(symbol)
    if (
        registry is not None
        and client.signing_registry is not None
        and (
            registry.endpoint != client.signing_registry.endpoint
            or registry.markets != client.signing_registry.markets
        )
    ):
        raise SafetyError("Supplied Endpoint context differs from public signing discovery")
    registry = registry or client.signing_registry
    if registry is None:
        raise SafetyError("Public signing discovery is required before preparing an order")
    intent = client.prepare_order(
        account.wallet,
        market,
        quote,
        subaccount=account.subaccount,
        endpoint_time=read_endpoint_time(rpc_url, registry),
    )
    print(f"Submitting client order {intent.client_order_id}, digest {intent.digest}")
    acknowledgement = client.submit_order(intent)
    order_id = acknowledgement["orderId"]
    print(f"Accepted order {order_id}; requesting one cancellation")
    client.cancel_order(market, order_id)
    # A cancellation receipt alone does not prove a terminal order projection.
    current = client.query_order(symbol, order_id=order_id)
    print(f"Order status after cancellation request: {current['status']}")
    if current["status"] not in {"CANCELED", "FILLED", "EXPIRED"}:
        raise SafetyError("Order is still nonterminal; inspect it before placing another")
    return current
