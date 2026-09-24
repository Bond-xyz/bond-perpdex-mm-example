# BondPerpDEX OEMS integration examples

Small Python examples for **Galileo testnet (chain 16602)**. Start with the
no-network signing example, then choose the credential and transport you need.
This repository is reference code, not a published SDK or proof that a live
account can currently trade. The private examples make real requests when run.

## Start here

Python 3.12+ and a POSIX shell are required.

```sh
sh install.sh
.venv/bin/python examples/01_signing_offline.py
```

The first script prints a synthetic SIWE message and Vertex order envelope.
It shows the **wallet EIP-712 signature** and **Ed25519 account signature**
without sending a request. Example 04 covers the separate provisioned
bot-key header signature.

| Step | Run or read | Network and effect |
| --- | --- | --- |
| 1. Sign an order | `examples/01_signing_offline.py` | None; synthetic fixture only. |
| 2. Inspect public data | `.venv/bin/python -m bond_perpdex_client --testnet-read-only` | Public REST and WebSocket reads. |
| 3. Sign in, read, optionally place/cancel | `examples/02_private_rest.py --help` | SIWE sign-in and private reads; `--place` sends one order. |
| 4. Subscribe to private updates | `examples/03_private_stream.py --help` | SIWE sign-in and bounded private stream; no orders. |
| 5. Use an existing bot key | `examples/04_provisioned_key_read.py` | One private REST account read; no orders. |

The scripts are separate so a reader can follow one protocol layer at a time.
The reusable client lives in `src/bond_perpdex_client/`; the maker strategy is an
independent offline demonstration in `python -m bond_perpdex_client`.
`bond_perpdex_client` is this repository's Python import name, not an official
`bond-sdk` package. The installable project is named `bond-perpdex-mm-example`.

The client code follows the same boundaries: `client_session.py` owns sign-in
and signed transport, `client_account.py` owns reads and reconciliation,
`client_orders.py` owns order commands, `user_stream.py` owns subscription
lifecycle, and `client.py` preserves one small public facade. `signing.py`
builds wallet and account signatures; `bot_key.py` builds the separate
provisioned-key HTTP headers.

## Direct answers for OEMS integrators

1. **Order signing:** The documented Galileo perp order path requires a
   `signedOrder` Vertex EIP-712 envelope as well as an Ed25519 account request
   signature. Ed25519 alone does not authorize a release-bound perp order.
   REST `POST /fapi/v1/order` is implemented here; side-effect-free
   `websocket_order_place()` shows the equivalent `order.place` request shape.
   WebSocket order transmission has not been live-verified by this repository.
2. **Binance compatibility:** Route names and selected fields are familiar, but
   private payloads and authentication are Bond-specific. See
   [event and response examples](docs/oems-integration.md#2-binance-compatibility-and-fees).
   The current engine models nonnegative execution fees in `USDC.e`; this
   example does not claim negative maker rebates.
3. **Authentication:** The SIWE wallet session used for private WebSocket and
   examples 02–03 is distinct from a provisioned REST bot key. The latter can
   be reused across process restarts while valid and not revoked, provided its
   Ed25519 key is retained securely. This package does not persist credentials.
4. **Rate limits:** `exchangeInfo` does not describe every applicable layer.
   See [rate limits](docs/oems-integration.md#4-rate-limits) for the bot-key,
   IP, and WebSocket limits and what is actually metered.

The [OEMS integration guide](docs/oems-integration.md) gives exact signing
fields, credential lifetimes, sanitized source-derived frames, and verification
limits. The [protocol reference](docs/protocol.md) holds implementation details.

## Operating boundaries

- Real private access must be explicitly enabled in code; real order writes
  additionally require `allow_live_orders=True`. Scripts 02–03 use a dedicated
  testnet wallet key supplied as `BOND_TESTNET_WALLET_KEY` by a secret manager.
  Script 04 reads existing `BOND_BOT_API_KEY` and
  `BOND_BOT_ED25519_PRIVATE_KEY_PEM`; it never creates a key.
- A lost submit or cancel acknowledgement is an **unknown outcome**, not a
  reason to send the mutation again. Query and reconcile venue state.
  Reconnect does not replay missed private events. The client has no durable
  journal, so it is not an unattended market maker.
- The example contract and VirtualBook registry come from `bond-perpdex`
  revision `34869838ed5588f6a4686f6663e824212287ddb3`. Check the deployed
  environment and current source before live use.

## Checks

```sh
.venv/bin/ruff format --check src tests examples
.venv/bin/ruff check src tests examples
.venv/bin/pytest -q
.venv/bin/python examples/01_signing_offline.py
```

Tests use blocked sockets/DNS and mocked transports. They verify signing
vectors and request shapes, not live admission, fills, fees, or deployment.
