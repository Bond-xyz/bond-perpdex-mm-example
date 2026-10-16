"""Read a bounded private stream using a wallet session; send no orders."""

import argparse
from contextlib import closing
from pathlib import Path

from bond_perpdex_client import (
    BondPerpDexClient,
    TestnetConfig,
    TestnetTransport,
    WalletSigner,
    load_ed25519_private_key,
)


def main() -> None:
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
    parser.add_argument("--max-events", type=int, default=5)
    parser.add_argument(
        "--idle-timeout", type=float, default=5, help="Seconds to wait for an event (1–60)"
    )
    args = parser.parse_args()

    config = TestnetConfig(allow_live_private=True)
    client = BondPerpDexClient(TestnetTransport(config))
    wallet = WalletSigner.from_environment()
    key = load_ed25519_private_key(args.ed25519_key)
    if args.session_file:
        client.authenticate_cached(wallet, key, args.subaccount, args.session_file)
    else:
        client.authenticate(wallet, key, args.subaccount)
    with closing(
        client.user_events(
            max_events=args.max_events, max_reconnects=0, idle_timeout=args.idle_timeout
        )
    ) as messages:
        for message in messages:
            if message.kind == "connected":
                print(f"Subscription acknowledged: {message.subscription_id}")
            elif message.kind == "event":
                event = message.event or {}
                # Print only protocol identifiers; avoid logging account payloads.
                print(f"Event {event.get('e')}: {event.get('i', event.get('eventId', ''))}")
            else:
                print(f"Stream disconnected: {message.reason}")
                # A quiet account may emit no events during the bounded wait.
                # Refresh REST state and let the operator decide when to reconnect.
                client.reconcile_account(args.symbol)
                print("REST account reconciliation completed")
                break
            if message.requires_reconciliation:
                client.reconcile_account(args.symbol)


if __name__ == "__main__":
    main()
