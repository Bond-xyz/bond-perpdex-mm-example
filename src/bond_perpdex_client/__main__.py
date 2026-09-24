"""Deterministic no-network maker demonstration; online mode is public reads only."""

import argparse
import json
import uuid
from pathlib import Path

from .client import BondPerpDexClient
from .models import SafetyError, TestnetConfig, UnknownOutcome, decimal
from .offline import DEPTH_FRAME, NOW_MS, OfflineVenue, demo_identity
from .strategy import DepthBook, MakerPolicy
from .transport import TestnetReadOnlyTransport


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--testnet-read-only", action="store_true")
    args = parser.parse_args()
    settings = json.loads(args.config.read_text()) if args.config else {}
    if settings.get("mode", "offline") != "offline":
        raise SafetyError(
            "The CLI supports only offline configuration; use the sample client for private I/O"
        )
    if settings.get("allow_live_private") or settings.get("allow_live_orders"):
        raise SafetyError("CLI never enables private I/O; explicitly configure the sample client")
    config = TestnetConfig(
        **{
            key: settings[key]
            for key in (
                "chain_id",
                "http_url",
                "ws_url",
                "ws_api_url",
                "max_order_notional",
                "max_position",
                "max_open_orders",
            )
            if key in settings
        }
    )
    config.validate()
    symbol = settings.get("symbol", "BTCUSDCPERP")
    if args.testnet_read_only:
        transport = TestnetReadOnlyTransport(config)
        client = BondPerpDexClient(transport, config=config)
        market = client.market(symbol)
        print(
            json.dumps(
                {
                    "mode": "testnet-public-read-only",
                    "symbol": market.symbol,
                    "depth": client.depth(symbol),
                }
            )
        )
        for frame in transport.depth_events(symbol):
            print(json.dumps(frame))
        return
    venue = OfflineVenue()
    client = BondPerpDexClient(venue, config=config, clock_ms=lambda: NOW_MS)
    wallet, key = demo_identity()
    subaccount = settings.get("subaccount", "bond")
    client.authenticate_offline(wallet, key, subaccount)
    market = client.market(symbol)
    if client.open_orders(symbol):
        raise SafetyError("Existing orders require reconciliation")
    positions = client.positions(symbol)
    if len(positions) != 1 or positions[0]["symbol"] != symbol:
        raise SafetyError("Missing or ambiguous position")
    book = DepthBook(symbol, settings.get("max_feed_age_ms", 2000))
    book.snapshot(client.depth(symbol))
    book.apply(DEPTH_FRAME, NOW_MS)
    policy = MakerPolicy(
        quantity=decimal(settings.get("quantity", "0.002")),
        half_spread_bps=settings.get("half_spread_bps", 10),
        max_position=decimal(settings.get("max_position", "0.010")),
        max_order_notional=decimal(settings.get("max_order_notional", "200")),
    )
    quotes = policy.quotes(
        market,
        book,
        position=decimal(positions[0]["positionAmt"]),
        position_observed_ms=NOW_MS,
        now_ms=NOW_MS,
        outstanding=[],
    )
    acknowledged = []
    try:
        for index, quote in enumerate(quotes):
            intent = client.prepare_order(
                wallet,
                market,
                quote,
                subaccount=subaccount,
                nonce=42 + index,
                client_order_id=str(uuid.uuid5(uuid.NAMESPACE_OID, f"offline-{index}")),
            )
            client.submit_offline(intent)
            acknowledged.append(intent.client_order_id)
            print(f"OFFLINE POST_ONLY {quote.side} {quote.quantity} {symbol} @ {quote.price}")
    finally:
        book.disconnect()
        failures = 0
        for client_id in acknowledged:
            try:
                client.cancel_offline(market, client_id)
            except (SafetyError, UnknownOutcome):
                failures += 1
        if failures:
            raise UnknownOutcome(f"Cleanup uncertain for {failures} orders; stop and reconcile")
    if client.open_orders(symbol):
        raise SafetyError("Offline cleanup did not complete")
    print(
        f"PASS: {len(quotes)} signed quotes, confirmed cancellations, no open orders; "
        "zero network/transactions."
    )


if __name__ == "__main__":
    main()
