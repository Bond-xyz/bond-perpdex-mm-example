"""Read a bounded private stream using a wallet session; send no orders."""

import argparse
from contextlib import closing

from bond_perpdex import BondPerpDexClient, TestnetConfig, TestnetTransport, WalletSigner


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTCUSDCPERP")
    parser.add_argument("--subaccount", default="bond")
    parser.add_argument("--max-events", type=int, default=5)
    args = parser.parse_args()

    config = TestnetConfig(allow_live_private=True)
    client = BondPerpDexClient(TestnetTransport(config))
    client.authenticate(WalletSigner.from_environment(), subaccount=args.subaccount)
    with closing(client.user_events(max_events=args.max_events, max_reconnects=2)) as messages:
        for message in messages:
            if message.kind == "connected":
                print(f"Subscription acknowledged: {message.subscription_id}")
            elif message.kind == "event":
                event = message.event or {}
                # Print only protocol identifiers; avoid logging account payloads.
                print(f"Event {event.get('e')}: {event.get('i', event.get('eventId', ''))}")
            else:
                print(f"Stream disconnected: {message.reason}")
            if message.requires_reconciliation and message.kind != "disconnected":
                client.reconcile_account(args.symbol)


if __name__ == "__main__":
    main()
