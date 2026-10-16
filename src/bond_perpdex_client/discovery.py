"""Read and validate public signing metadata without credentials or order writes."""

import json

from .client import BondPerpDexClient


def main() -> None:
    registry = BondPerpDexClient().discover_signing_context()
    print(
        json.dumps(
            {
                "chainId": 16602,
                "endpoint": registry.endpoint,
                "markets": {
                    symbol: {"productId": product, "virtualBook": book}
                    for symbol, (product, book) in registry.markets.items()
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
