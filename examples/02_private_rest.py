"""Supervised wallet-session sign-in, account read, and optional REST order.

Use a dedicated funded testnet account. Running this script signs in and reads
the account. ``--place`` additionally sends one order and attempts one cancel.
No market price is supplied by the example: the operator must choose one.
"""

import argparse
from decimal import Decimal

from bond_perpdex import (
    BondPerpDexClient,
    Quote,
    SafetyError,
    TestnetConfig,
    TestnetTransport,
    WalletSigner,
)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTCUSDCPERP")
    parser.add_argument("--subaccount", default="bond")
    parser.add_argument("--place", action="store_true")
    parser.add_argument("--side", choices=["BUY", "SELL"])
    parser.add_argument("--price", type=Decimal)
    parser.add_argument("--quantity", type=Decimal)
    args = parser.parse_args()
    if args.place and (args.side is None or args.price is None or args.quantity is None):
        parser.error("--place requires --side, --price, and --quantity")
    if not args.place and any(
        value is not None for value in (args.side, args.price, args.quantity)
    ):
        parser.error("order fields require --place")
    return args


def main() -> None:
    args = arguments()
    config = TestnetConfig(allow_live_private=True, allow_live_orders=args.place)
    client = BondPerpDexClient(TestnetTransport(config))
    wallet = WalletSigner.from_environment()

    # SIWE creates a wallet session. A new sign-in may replace its old token.
    client.authenticate(wallet, subaccount=args.subaccount)
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
