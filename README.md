# BondPerpDEX OEMS integration examples

Small Python examples for **Galileo testnet (chain 16602)**. Start offline.
Private examples make real requests; order writes require explicit permission.
This is reference source, not a published SDK or an unattended market maker.

## Quick start with editable defaults

```sh
make quickstart
```

This installs dependencies if needed, creates an ignored owner-only `.env`, and
runs the offline maker with `tradegods-z` and the example strategy/risk defaults.
Use `make config` to inspect effective settings. Edit `.env`
or override values in your process environment to tune the example.

```sh
BOND_HALF_SPREAD_BPS=20 BOND_QUANTITY=0.003 make quickstart
make setup ARGS=--generate-signing-key
make private
```

Private modes require your wallet credentials. Live trade mode additionally needs
funding, public signing discovery, an operator-selected price and the explicit
`--allow-live-orders` flag. Endpoint time defaults to the public Galileo RPC. The [configuration guide](docs/configuration.md)
explains every variable, default, consuming module, mode and setup step.

## Individual examples

Python 3.12+, Make and a POSIX shell are required. Use `make help` for targets
and pass additional CLI flags through `ARGS`. Shell helpers live in `scripts/`;
the Make targets handle their working directory and environment setup.

```sh
make install
make signing
make offline
make bot-headers
```

These commands send no requests. The signing vector and offline maker use
synthetic contracts and public simulation identities. They are never live defaults.
The installable project is `bond-perpdex-mm-example`; its Python import is
`bond_perpdex_client`.

| Example | Purpose and effect |
| --- | --- |
| `01_signing_offline.py` | Synthetic SIWE, wallet EIP-712 order, Ed25519 account signature and a WebSocket order frame; no network. |
| `02_private_rest.py` | SIWE account reads; `--place` additionally sends one REST order and attempts cancellation. |
| `03_private_stream.py` | Timestamp-bound `userDataStream.subscribe.signature`; bounded private events, no orders. |
| `04_provisioned_key_headers.py` | Synthetic managed-key trading REST headers; no credentials read and no network. |
| `make discover` | Public signing metadata validation; no credentials or orders. |
| `make public` | Public exchangeInfo, depth and bounded market-stream reads; no credentials or orders. |

## Select your account and retain its credentials

The website enables trading and deposits into each wallet's **`bond`** subaccount.
A maker can use **`tradegods-z`** instead. Names are scoped to the owner wallet,
not globally reserved; a new name has separate balances. Live sign-in requires
an explicit name and persistent Ed25519 key. It never silently selects `bond`.

Provision the owner wallet through `BOND_TESTNET_WALLET_KEY` in your secret manager.
Create an Ed25519 PKCS#8 PEM through your key manager, retain it outside this repo,
and restrict its permissions to `0600`. Then run:

```sh
make rest-example \
  ARGS="--subaccount tradegods-z --ed25519-key /secure/galileo-ed25519.pem \
  --session-file /secure/galileo.session.json"

make stream-example \
  ARGS="--subaccount tradegods-z --ed25519-key /secure/galileo-ed25519.pem \
  --session-file /secure/galileo.session.json --max-events 5 --idle-timeout 15"
```

The optional session file securely caches the token, account UUID, public-key
identity and expiry; it contains no private key. A matching valid session is
checked with `/auth/me` and reused across restarts. At **24 hours or less** before
expiry, this explicit startup/renewal call signs in again with the persistent key.
A reader error or identity mismatch stops execution without replacing credentials.
Without `--session-file`, every run signs in again and replaces the prior token.
The stream's event idle wait defaults to five seconds; `--idle-timeout` selects
a bounded 1–60 second wait. Subscription acknowledgements retain a five-second
deadline. A quiet stream ends with REST reconciliation.

Use one renewal owner per subaccount. Close streams, pause submissions and reconcile
pending/uncertain commands before renewal, then reconnect, subscribe and reconcile
REST state. There is no automatic daemon renewal or replay. Protect the cache's
parent directory; do not commit, log or back up credentials.

## Supply current signing context before trading

The client reads public `GET /fapi/v1/exchangeInfo`: top-level `chainId` and
`endpoint`, and each symbol's `productId` and `virtualBook`, alongside its filters.
No private repository or registry file is required. Check discovery without
credentials or order writes:

```sh
make discover
```

The server derives these fields from its validated release binding. The client
rejects missing fields, wrong chains, invalid/duplicate mappings and changes
during a run. `BOND_REGISTRY_FILE` / `--registry` are optional operator pins,
checked against discovery. They are not a fallback when discovery is unavailable.
During rollout, a server without the extension cannot run this version's trade
mode; private reads and offline mode remain available.

Only after funding the chosen subaccount and setting your price and quantity
in the configured environment, run:

```sh
make trade ARGS=--allow-live-orders
```

`make trade` alone rejects execution. For the individual REST example, pass
`--place`, side, price and quantity (optionally `--rpc-url`) through `ARGS`; use
`make rest-example ARGS=--help` for its required account and order flags.

Live order preparation checks Galileo RPC chain identity and reads the verified
Endpoint's `getTime()` for signed expiry. The RPC defaults to the public
`https://evmrpc-testnet.0g.ai` (chain 16602); `BOND_GALILEO_RPC_URL` overrides it.
Expiry is 60 seconds after the later of Endpoint time and the client clock:
ingress also checks wall time, while Endpoint time can lag between settlement
batches. Expiry ends a resting order and amendment keeps it; extend by cancel and
new order. The client validates live precision, tick/step/notional filters and its
configured risk bounds. Orders can execute.

Signed orders encode LIMIT/GTC, LIMIT/IOC, LIMIT/FOK or POST_ONLY/GTC, plus
IOC/FOK reduce-only, in `expiration`; signed MARKET is unsupported. Use
`prepare_marketable_order()` for a band-bounded LIMIT/IOC. See
[order modes, price band and position cap](docs/oems-integration.md#signed-order-modes-prices-and-limits).
No deposits are implemented; the website cannot select a named account to fund.
See [funding and all six integration answers](docs/oems-integration.md).

## Deployment limits

- Use SIWE sessions for private events and the full OEMS read/trade flow. Managed
  `x-bond-api-key` credentials remain REST-only; current public account-query
  routes require SIWE. Example 04 therefore demonstrates headers offline.
- Galileo's public **REST cancel-all returns 405**. Live `cancel_all()` sends a signed
  WebSocket `openOrders.cancelAll` instead. Cancel-one remains signed REST. Both
  carry a distinct cancellation receipt key; no mutation is automatically replayed.
- The 120-per-minute managed-key counter does not apply to SIWE requests. Trading
  WebSocket commands have no implemented fixed command counter or shared REST/WS
  command counter in the inspected backend. Capacity and admission still apply.
  See [exact limits](docs/oems-integration.md#5-which-rate-limits-apply).

## Checks and evidence

```sh
make check
make signing
```

Tests block sockets/DNS and use mock transports. The dated
[Galileo verification summary](docs/galileo-live-verification.md) covers named-account
deposit credit, place/cancel and private events, with zero fills. Offline tests do
not establish funded behavior. Fill/fee behavior and sustained throughput remain
untested.
