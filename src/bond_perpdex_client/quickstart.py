"""Run the configured offline maker or an explicitly selected Galileo example."""

import argparse
from contextlib import closing
from pathlib import Path

from . import (
    BondPerpDexClient,
    Quote,
    SigningRegistry,
    TestnetTransport,
    WalletSigner,
    load_ed25519_private_key,
    read_endpoint_time,
)
from .__main__ import run as run_demonstration
from .configuration import MODES, Settings
from .models import SafetyError
from .setup import ensure_environment


def run(settings: Settings, *, allow_live_orders: bool = False) -> None:
    settings.validate()
    mode = settings.mode
    if mode == "trade" and not allow_live_orders:
        raise SafetyError("Trade mode requires the explicit --allow-live-orders command flag")
    if allow_live_orders and mode != "trade":
        raise SafetyError("--allow-live-orders is only valid with trade mode")
    symbol, name = settings.values["BOND_SYMBOL"], settings.values["BOND_SUBACCOUNT"]
    print(f"Mode: {mode}; subaccount: {name}; market: {symbol}")
    if mode in {"offline", "public"}:
        run_demonstration(settings.legacy_settings(), public_read_only=mode == "public")
        return

    registry = None
    if mode == "trade":
        if not settings.values["BOND_LIMIT_PRICE"] or not settings.values["BOND_GALILEO_RPC_URL"]:
            raise SafetyError("Trade mode requires BOND_LIMIT_PRICE and BOND_GALILEO_RPC_URL")
        registry = SigningRegistry.from_file(settings.path("BOND_REGISTRY_FILE"))
    wallet = WalletSigner.from_environment(settings.values)
    key = load_ed25519_private_key(settings.path("BOND_ED25519_PRIVATE_KEY_FILE"))
    config = settings.client_config(allow_orders=mode == "trade", registry=registry)
    client = BondPerpDexClient(TestnetTransport(config))
    client.authenticate_cached(wallet, key, name, settings.path("BOND_SESSION_FILE"))
    identity = client.identity()
    print(f"Backend subaccount UUID: {identity['id']}; encoded name: {identity['name']}")
    snapshot = client.reconcile_account(symbol)
    print(
        f"REST snapshot: {len(snapshot.positions)} positions; "
        f"{len(snapshot.open_orders)} open orders"
    )
    if mode == "private":
        return
    if mode == "stream":
        with closing(
            client.user_events(
                max_events=settings.integer("BOND_MAX_EVENTS", 1, 10_000),
                max_reconnects=settings.integer("BOND_MAX_RECONNECTS", 0, 3),
                idle_timeout=settings.integer("BOND_STREAM_IDLE_TIMEOUT_SECONDS", 1, 60),
            )
        ) as messages:
            for message in messages:
                print(f"Private stream: {message.kind}; {message.reason}")
                if message.requires_reconciliation:
                    client.reconcile_account(symbol)
        return

    market = client.market(symbol)
    intent = client.prepare_order(
        wallet,
        market,
        Quote(
            settings.values["BOND_ORDER_SIDE"],
            settings.positive("BOND_LIMIT_PRICE"),
            settings.positive("BOND_QUANTITY"),
        ),
        subaccount=name,
        endpoint_time=read_endpoint_time(settings.values["BOND_GALILEO_RPC_URL"], registry),
    )
    print(f"Retain order intent: {intent.client_order_id}; digest: {intent.digest}")
    accepted = client.submit_order(intent)
    order_id = accepted["orderId"]
    print(f"Accepted order: {order_id}; requesting cancellation")
    client.cancel_order(market, order_id)
    terminal = client.query_order(symbol, order_id=order_id)
    print(f"Order status: {terminal['status']}")
    if terminal["status"] not in {"CANCELED", "FILLED", "EXPIRED"}:
        raise SafetyError("Order remains nonterminal; reconcile before placing another")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--mode", choices=MODES, help="Override BOND_MODE for this run")
    parser.add_argument(
        "--show-config", action="store_true", help="Show settings with secrets redacted; do not run"
    )
    parser.add_argument(
        "--allow-live-orders", action="store_true", help="Explicitly allow one supervised trade"
    )
    args = parser.parse_args()
    if ensure_environment(args.env_file):
        print(f"Created default environment: {args.env_file}")
    settings = Settings.load(args.env_file)
    if args.mode is not None:
        settings.values["BOND_MODE"] = args.mode
    settings.validate()
    if args.show_config:
        print(settings.redacted_json())
        return
    run(settings, allow_live_orders=args.allow_live_orders)


if __name__ == "__main__":
    main()
