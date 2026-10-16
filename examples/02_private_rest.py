"""Supervised wallet-session sign-in, account read, and optional REST order.

Use a dedicated funded testnet account. Running this script signs in and reads
the account. ``--place`` additionally sends one order and attempts one cancel.
No market price is supplied by the example: the operator must choose one.
"""

import argparse
from decimal import Decimal
from pathlib import Path

from bond_perpdex_client import (
    BondPerpDexClient,
    Quote,
    SafetyError,
    SigningRegistry,
    TestnetConfig,
    TestnetTransport,
    WalletSigner,
    load_ed25519_private_key,
    read_endpoint_time,
)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTCUSDCPERP")
    parser.add_argument("--subaccount", required=True)
    parser.add_argument(
        "--ed25519-key", type=Path, required=True, help="Owner-only persistent Ed25519 PKCS#8 PEM"
    )
    parser.add_argument(
        "--session-file",
        type=Path,
        help="Optional owner-only session cache; renew within 24 hours of expiry",
    )
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
    client = BondPerpDexClient(TestnetTransport(config))
    wallet = WalletSigner.from_environment()

    # SIWE creates a wallet session. A new sign-in may replace its old token.
    key = load_ed25519_private_key(args.ed25519_key)
    if args.session_file:
        client.authenticate_cached(wallet, key, args.subaccount, args.session_file)
    else:
        client.authenticate(wallet, key, args.subaccount)
    identity = client.identity()
    print(f"Backend subaccount UUID: {identity['id']}; encoded name: {identity['name']}")
    account = client.account()
    print(f"PerpDEX account present: {account.get('accountPresent')}")
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

    market = client.market(args.symbol)
    intent = client.prepare_order(
        wallet,
        market,
        Quote(args.side, args.price, args.quantity),
        subaccount=args.subaccount,
        endpoint_time=read_endpoint_time(args.rpc_url, registry),
    )
    print(f"Submitting client order {intent.client_order_id}, digest {intent.digest}")
    acknowledgement = client.submit_order(intent)
    order_id = acknowledgement["orderId"]
    print(f"Accepted order {order_id}; requesting one cancellation")
    # A cancel receipt can omit status. Only the follow-up query proves terminal state.
    client.cancel_order(market, order_id)
    current = client.query_order(args.symbol, order_id=order_id)
    print(f"Order status after cancellation request: {current['status']}")
    if current["status"] not in {"CANCELED", "FILLED", "EXPIRED"}:
        raise SafetyError("Order is still nonterminal; inspect it before placing another")


if __name__ == "__main__":
    main()
