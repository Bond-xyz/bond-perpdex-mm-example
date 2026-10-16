# Configure the maker example

`make quickstart` installs the Python environment if needed, creates an owner-only
`.env` with the example defaults, and runs the offline maker. Its account name is
`tradegods-z`, with two-sided BTC quotes, quantity `0.002`, half-spread `10` bps,
and the documented risk bounds. This completes without credentials or network
requests after dependency installation.

```sh
make quickstart
make config
```

To prepare configuration separately, optionally choose a different name:

```sh
make setup ARGS="--subaccount my-maker"
```

Setup never overwrites an existing `.env`, even when passed `--subaccount`.
Edit the file to change an existing configuration. The checked-in `.env.example`
contains the public defaults and blank secret inputs. Setup never copies wallet
secrets from your shell into that file. `.env` is ignored and created with mode
`0600`; keep its parent directory private. A manually copied file also needs
`chmod 600 .env`.

## Choose the layer to run

| Mode | Command | Behavior |
| --- | --- | --- |
| Offline maker (default) | `make quickstart` | Deterministic BTC book, quote strategy, signed simulated orders and cleanup; no venue I/O. Spread, quantity, position/notional and freshness settings feed this strategy. |
| Public data | `make public` | Bounded Galileo REST/depth stream reads; no wallet needed. |
| Private account | `make private` | SIWE/session reuse, account UUID and REST position/open-order snapshot; no order writes. |
| Private events | `make stream` | Signed private subscription, bounded events/reconnects and REST reconciliation; no order writes. |
| Supervised order | `make trade ARGS=--allow-live-orders` | One operator-priced order, cancellation attempt and terminal-state query. Requires the funded named account, wallet/key, public signing discovery and a verified RPC. This does not run a continuous live quoting strategy. |

Changing `BOND_MODE=trade` alone cannot place an order: the command flag is also
required. Public/private modes do not run the offline quoting policy. Live orders
use the configured side, price and quantity and the client's risk checks. The
example does not provision collateral, implement a daemon scheduler or restore
command state after a process restart.

The individual `examples/` scripts retain their explicit flags and process-environment
inputs; they do not automatically load `.env`. The older `config.example.json`
configures `make offline ARGS="--config ..."` only. Use the quick-start
runner for the unified dotenv workflow; it does not merge the JSON configuration.

The individual scripts and quick-start profiles share credential setup and
supervised trade/stream flows in `workflows.py`. `credentials.py` owns cache
identity checks and token-file serialization; `client_session.py` coordinates
authentication and renewal. The cache format and explicit permission flags are
the same across these entry points.

A quiet stream reconciles REST after its final disconnect and exits cleanly at
the configured reconnect budget. Subscription rejection, invalid events and
failed REST reconciliation still stop the run with an error. These bounded
profiles do not run a persistent reconnect loop.

## Supply account credentials and signing context

Before private modes, supply `BOND_TESTNET_WALLET_KEY` through a secret manager
(or the owner-only `.env`), and create a persistent Ed25519 key:

```sh
make setup ARGS=--generate-signing-key
make private
```

The key is generated locally at `BOND_ED25519_PRIVATE_KEY_FILE`, with mode `0600`;
an existing valid key is preserved. No wallet or venue account is created by
setup. The selected private mode uses SIWE to register/reuse that wallet/name.
The session cache contains a token and public identity, never the private key.
It reuses valid sessions and renews explicitly at 24 hours remaining, using the
behavior in [the OEMS guide](oems-integration.md#6-which-credentials-should-a-long-running-oems-use).

For trade mode, also set `BOND_LIMIT_PRICE` and `BOND_GALILEO_RPC_URL`.
Product IDs, VirtualBooks and the Endpoint are discovered from public
`exchangeInfo`; no private registry file is required. Run `make discover` first.
`BOND_REGISTRY_FILE` is an optional operator pin checked against the public
response. Missing discovery fields or mismatches stop trading; deploy the
exchangeInfo extension before using this mode. The RPC checks chain 16602 and
reads Endpoint time. The website funds your wallet's `bond` subaccount; a named
account such as `tradegods-z` needs its own funding.

## Override settings and run separate instances

Precedence is **command flags → process environment → `.env` → built-in defaults**.
Pass CLI flags through Make's `ARGS` variable. Only mode and live-order permission
have runner flags; `--env-file` selects the configuration file. Dotenv values are
parsed as literal data: optional quotes, comments and `export KEY=value` are supported;
shell commands, interpolation and variable expansion are never executed.
Unknown or duplicate keys in `.env` are rejected. `--show-config` prints effective
settings with wallet and RPC inputs redacted, and performs no venue requests.

```sh
BOND_HALF_SPREAD_BPS=20 BOND_QUANTITY=0.003 make quickstart
make setup ARGS="--env-file makers/team-z/.env --subaccount team-z"
make quickstart ARGS="--env-file makers/team-z/.env"
```

Relative registry, key and session paths resolve beside the selected environment
file. Use a separate directory/name/key/cache per independent maker. Keep shared
subaccount use coordinated: a new SIWE sign-in can invalidate another process's
token. Never restart to bypass an uncertain-order halt.

## Make targets and shell helpers

Run commands from the repository root, or use `make -C /path/to/repo <target>`.
The helpers under `scripts/` resolve the repository root before touching `.venv`
or configuration. `make install` explicitly installs/updates dependencies; the
setup and quick-start targets bootstrap automatically when the local environment
is absent.
`make check` runs tests, lint, format, dependency and shell-syntax checks.

Generate the public template and variable reference through Make:

```sh
make env-template > .env.example
make env-reference
```

`make trade` still requires `ARGS=--allow-live-orders`; selecting the target or
setting `BOND_MODE=trade` alone does not grant order permission.

## Variables and consuming modules

The following table and `.env.example` are generated from `configuration.SETTINGS`.
Venue identity values are exposed for visibility and validation; this example
rejects different chain IDs or venue URLs. Spread/freshness are offline strategy
parameters; live command throughput and session-renewal timing are not tuning
knobs in this runner. Refer to the [venue limits](oems-integration.md#5-which-rate-limits-apply)
for the applicable protections.

<!-- BEGIN GENERATED VARIABLES -->
| Variable | Default | Layer / module | Effect |
| --- | --- | --- | --- |
| `BOND_MODE` | `offline` | Runtime: `quickstart` | offline, public, private, stream or trade; trade also needs --allow-live-orders. |
| `BOND_SUBACCOUNT` | `tradegods-z` | Account: `client_session / signing` | Wallet-scoped name (1-12 ASCII bytes, no spaces); separate funding from website bond. |
| `BOND_TESTNET_WALLET_KEY` | Empty; maker supplied | Account: `signing` | Owner wallet secret; inject via a secret manager. Required for private modes. |
| `BOND_ED25519_PRIVATE_KEY_FILE` | `.local/galileo-ed25519.pem` | Authentication: `credentials` | Persistent owner-only Ed25519 PKCS#8 PEM. setup --generate-signing-key can create it. |
| `BOND_SESSION_FILE` | `.local/galileo.session.json` | Authentication: `client_session` | Owner-only token cache. Reuse a matching session; explicitly renew at 24 hours remaining. |
| `BOND_CHAIN_ID` | `16602` | Venue: `models / transport` | Galileo identity check; this example accepts only 16602. |
| `BOND_HTTP_URL` | `https://perpdex-testnet.bond.xyz` | Venue: `transport` | Pinned Galileo REST origin; alternate origins are rejected. |
| `BOND_PUBLIC_WS_URL` | `wss://perpdex-testnet.bond.xyz/ws` | Venue: `transport` | Pinned public market-data WebSocket; alternate URLs are rejected. |
| `BOND_TRADING_WS_URL` | `wss://perpdex-testnet.bond.xyz/ws-fapi/v1` | Venue: `transport` | Pinned trading/private-event WebSocket; alternate URLs are rejected. |
| `BOND_REGISTRY_FILE` | Empty; maker supplied | Signing context: `registry` | Optional operator pin; trade discovers public exchangeInfo signing context and checks this file if set. |
| `BOND_GALILEO_RPC_URL` | Empty; maker supplied | Signing context: `chain` | Verified HTTPS Galileo RPC for chain identity and Endpoint time; required for trade. |
| `BOND_SYMBOL` | `BTCUSDCPERP` | Strategy: `models / strategy` | BTCUSDCPERP offline; BTCUSDCPERP, ETHUSDCPERP, SOLUSDCPERP or 0GUSDCPERP live. |
| `BOND_QUANTITY` | `0.002` | Strategy: `strategy / client_orders` | Base size per quote/order; subject to venue lot and min-notional filters. |
| `BOND_HALF_SPREAD_BPS` | `10` | Strategy: `strategy` | Offline maker half-spread in basis points, 1-1000. Not used to choose a live order price. |
| `BOND_ORDER_SIDE` | `BUY` | Supervised order: `client_orders` | BUY or SELL for trade mode only. |
| `BOND_LIMIT_PRICE` | Empty; maker supplied | Supervised order: `client_orders` | Operator-chosen live limit price, required for trade; no example price is used. |
| `BOND_MAX_POSITION` | `0.010` | Risk: `strategy / client_orders` | Maximum absolute per-market base position, including worst-case open-order fills. |
| `BOND_MAX_ORDER_NOTIONAL` | `200` | Risk: `strategy / client_orders` | Maximum quote-currency notional per order. |
| `BOND_MAX_OPEN_ORDERS` | `2` | Risk: `client_orders` | Live per-market open-order cap, 1-20; includes this account's other processes. |
| `BOND_MAX_FEED_AGE_MS` | `2000` | Risk: `strategy` | Offline book/inventory freshness bound in milliseconds, 1-10000. |
| `BOND_MAX_EVENTS` | `5` | Private stream: `user_stream` | Maximum delivered private events, 1-10000. |
| `BOND_MAX_RECONNECTS` | `0` | Private stream: `user_stream` | Bounded private-stream reconnect budget, 0-3; never replays orders or renews SIWE. |
| `BOND_STREAM_IDLE_TIMEOUT_SECONDS` | `5` | Private stream: `user_stream` | Event idle wait in seconds, 1-60; a quiet stream ends with REST reconciliation. |
<!-- END GENERATED VARIABLES -->
