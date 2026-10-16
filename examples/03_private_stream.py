"""Read a bounded private stream using a wallet session; send no orders."""

import argparse

from bond_perpdex_client import TestnetConfig
from bond_perpdex_client.workflows import add_credential_arguments, open_account, watch_account


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTCUSDCPERP")
    add_credential_arguments(parser)
    parser.add_argument("--max-events", type=int, default=5)
    parser.add_argument(
        "--idle-timeout", type=float, default=5, help="Seconds to wait for an event (1–60)"
    )
    return parser.parse_args()


def main() -> None:
    args = arguments()
    account = open_account(
        TestnetConfig(allow_live_private=True),
        subaccount=args.subaccount,
        key_file=args.ed25519_key,
        session_file=args.session_file,
    )
    watch_account(
        account.client, args.symbol, max_events=args.max_events, idle_timeout=args.idle_timeout
    )


if __name__ == "__main__":
    main()
