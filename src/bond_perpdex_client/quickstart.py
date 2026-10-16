"""Run the configured offline maker or an explicitly selected Galileo example."""

import argparse
from pathlib import Path

from . import Quote, SigningRegistry
from .__main__ import run as run_demonstration
from .configuration import MODES, Settings
from .models import SafetyError
from .setup import ensure_environment
from .workflows import open_account, place_and_cancel, print_identity, watch_account


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
        if settings.values["BOND_REGISTRY_FILE"]:
            registry = SigningRegistry.from_file(settings.path("BOND_REGISTRY_FILE"))
    account = open_account(
        settings.client_config(allow_orders=mode == "trade", registry=registry),
        subaccount=name,
        key_file=settings.path("BOND_ED25519_PRIVATE_KEY_FILE"),
        session_file=settings.path("BOND_SESSION_FILE"),
        environment=settings.values,
    )
    client = account.client
    print_identity(client)
    snapshot = client.reconcile_account(symbol)
    print(
        f"REST snapshot: {len(snapshot.positions)} positions; "
        f"{len(snapshot.open_orders)} open orders"
    )
    if mode == "private":
        return
    if mode == "stream":
        watch_account(
            client,
            symbol,
            max_events=settings.integer("BOND_MAX_EVENTS", 1, 10_000),
            max_reconnects=settings.integer("BOND_MAX_RECONNECTS", 0, 3),
            idle_timeout=settings.integer("BOND_STREAM_IDLE_TIMEOUT_SECONDS", 1, 60),
        )
        return

    place_and_cancel(
        account,
        symbol,
        Quote(
            settings.values["BOND_ORDER_SIDE"],
            settings.positive("BOND_LIMIT_PRICE"),
            settings.positive("BOND_QUANTITY"),
        ),
        rpc_url=settings.values["BOND_GALILEO_RPC_URL"],
        registry=registry,
    )


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
