"""Supervised wallet-session sign-in, account read, and optional REST order.

Use a dedicated funded testnet account. Running this script signs in and reads
the account. ``--place`` additionally sends one order and attempts one cancel.
No market price is supplied by the example: the operator must choose one.
"""

import argparse
from decimal import Decimal
from pathlib import Path

from bond_perpdex_client import Quote, SigningRegistry, TestnetConfig
from bond_perpdex_client.workflows import (
    add_credential_arguments,
    open_account,
    place_and_cancel,
    print_identity,
)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTCUSDCPERP")
    add_credential_arguments(parser)
    parser.add_argument("--place", action="store_true")
    parser.add_argument(
        "--registry", type=Path, help="Operator-verified canonical Galileo perpdex.json"
    )
    parser.add_argument("--rpc-url", help="Verified HTTPS Galileo RPC for Endpoint time")
    parser.add_argument("--side", choices=["BUY", "SELL"])
    parser.add_argument("--price", type=Decimal)
    parser.add_argument("--quantity", type=Decimal)
    args = parser.parse_args()
    if args.place and any(
        value is None
        for value in (args.side, args.price, args.quantity, args.registry, args.rpc_url)
    ):
        parser.error("--place requires --side, --price, --quantity, --registry, and --rpc-url")
    if not args.place and any(
        value is not None for value in (args.side, args.price, args.quantity)
    ):
        parser.error("order fields require --place")
    return args


def main() -> None:
    args = arguments()
    registry = SigningRegistry.from_file(args.registry) if args.registry else None
    config = TestnetConfig(
        allow_live_private=True, allow_live_orders=args.place, signing_registry=registry
    )
    account = open_account(
        config,
        subaccount=args.subaccount,
        key_file=args.ed25519_key,
        session_file=args.session_file,
    )
    client = account.client
    print_identity(client)
    balances = client.account()
    print(f"PerpDEX account present: {balances.get('accountPresent')}")
    rates = client.commission_rate(args.symbol)
    print(
        f"Advertised {args.symbol} rates: maker {rates.get('makerCommissionRate')}, "
        f"taker {rates.get('takerCommissionRate')}"
    )
    snapshot = client.reconcile_account(args.symbol)
    print(
        f"Account read: {len(snapshot.positions)} position, {len(snapshot.open_orders)} open orders"
    )
    if not args.place:
        return

    place_and_cancel(
        account,
        args.symbol,
        Quote(args.side, args.price, args.quantity),
        rpc_url=args.rpc_url,
        registry=registry,
    )


if __name__ == "__main__":
    main()
