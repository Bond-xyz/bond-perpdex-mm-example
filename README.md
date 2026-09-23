# BondPerpDex Python market-maker integration example

A small reusable Python package with a `BondPerpDexClient`, exact protocol signing,
and a one-level market-maker example. **Testnet only; live authentication and
transactions require separate explicit SDK permissions and credentials.** The default
CLI run is deterministic and entirely in memory. This is an SDK seed and teaching
example, not a production trading bot or an officially published SDK.

## Install and run

Prerequisites: Python **3.12+** and a POSIX shell. The installer locates an available
Python 3.12/3.13/3.14 (including Homebrew on this Mac), creates `.venv`, and installs
the package and development tools there. It does not change global Python, require
shell activation, or use the macOS system Python 3.9. Dependency installation may
contact the Python package index. All following commands run from this directory.

One install command:

```sh
sh install.sh
```

One dry-run command:

```sh
.venv/bin/python -m bond_perpdex
```

Expected output:

```text
OFFLINE POST_ONLY BUY 0.002 BTCUSDCPERP @ 64939.90
OFFLINE POST_ONLY SELL 0.002 BTCUSDCPERP @ 65070.10
PASS: 2 signed quotes, confirmed cancellations, no open orders; zero network/transactions.
```

No account, wallet, environment variables, blockchain RPC, database, companion
repository, or running server is needed. The offline wallet and session are
**public deterministic demo identities**, generated from conspicuous test labels.
Never fund them. No private keys, signatures, API keys, or request bodies are logged.

`config.example.json` is directly runnable with
`.venv/bin/python -m bond_perpdex --config config.example.json`. Its credential
placeholder is informational and unused by the CLI: credentials never belong in
the configuration file. The SDK's explicit `WalletSigner.from_environment()` loader
is described below; the CLI never calls it. Monetary settings are decimal strings.
The default fixture supports BTC only; registry mappings for ETH, SOL and 0G are
included for optional public reads, not invented simulation data.

Optional **networked public testnet reads**, with no authentication or orders:

```sh
.venv/bin/python -m bond_perpdex --testnet-read-only
```

This fetches `exchangeInfo` and a depth snapshot, then receives one acknowledged
WebSocket depth event and exits. It is not the smoke test and was **not run** during
development. HTTP and WebSocket URLs are exact allowlists from the source registry:
`https://perpdex-testnet.bond.xyz` and `wss://perpdex-testnet.bond.xyz/ws`, chain 16602.
Alternate hosts, ports, schemes, paths, credentials in URLs, and production chain
IDs fail closed. HTTP redirects and environment proxies are disabled; the pinned
synchronous WebSocket client does not follow redirects and has proxies disabled.
Timeouts, bounded message sizes/queues, and no automatic retries apply.

## Reuse the package

The public API is independent of the CLI and strategy. `Market`, `Quote`,
`OrderIntent`, `TestnetConfig`, and exceptions are exported with type annotations;
the package includes `py.typed`. Raw evolving server objects use the `Json` alias.
`signing.py` and `transport.py` have no strategy dependencies.

```python
from decimal import Decimal

from bond_perpdex import BondPerpDexClient, Quote
from bond_perpdex.offline import NOW_MS, OfflineVenue, demo_identity

venue = OfflineVenue()
client = BondPerpDexClient(venue, clock_ms=lambda: NOW_MS)
wallet, session_key = demo_identity()
client.authenticate_offline(wallet, session_key, subaccount="bond")
market = client.market("BTCUSDCPERP")
intent = client.prepare_order(
    wallet, market, Quote("BUY", Decimal("64939.9"), Decimal("0.002"))
)
ack = client.submit_offline(intent)
client.cancel_offline(market, intent.client_order_id)
assert client.open_orders(market.symbol) == []
```

`BondPerpDexClient()` without an injected transport provides only public testnet
reads. `TestnetReadOnlyTransport` always rejects auth, credentials, and writes,
even if passed an enabled configuration. `TestnetTransport` implements the real
private paths and independently enforces its permissions. The `_offline` methods
remain compatibility wrappers that categorically reject real transports; the
general methods below work with both the offline venue and an explicitly enabled
real transport. An injected transport is trusted Python code: dishonestly marking
a custom network transport `offline=True` circumvents the safety boundary and is
unsupported. Funding, deposits, transfers, leverage changes, and withdrawals are
not implemented.

## Explicitly enable the real testnet SDK

**These snippets make real requests if you run them; none was run during
development.** Use a separately provisioned, dedicated testnet wallet/subaccount.
Inject `BOND_TESTNET_WALLET_KEY` (32-byte hex) through your secret manager into the
process environment. Do not paste a key into a shell command, configuration file,
source file, notebook, or chat. No `.env` loading or credential persistence exists.
The wallet, fresh Ed25519 private session key, and resulting API token stay in
memory; Python cannot promise secure zeroization. Never enable debug wire logging.

Authentication and private reads/notifications are independently enabled from
order mutations:

```python
from contextlib import closing

from bond_perpdex import BondPerpDexClient, TestnetConfig, TestnetTransport, WalletSigner

config = TestnetConfig(allow_live_private=True, allow_live_orders=False)
client = BondPerpDexClient(TestnetTransport(config))
wallet = WalletSigner.from_environment()
client.authenticate(wallet, subaccount="bond")
snapshot = client.reconcile_account("BTCUSDCPERP")

with closing(client.user_events(max_events=20, max_reconnects=2)) as notifications:
    for message in notifications:
        if message.requires_reconciliation and message.kind != "disconnected":
            snapshot = client.reconcile_account("BTCUSDCPERP")
```

`authenticate()` performs real `GET /auth/nonce` and `POST /auth/signin`, generating
an Ed25519 key unless one is supplied explicitly. An ambiguous sign-in clears the
old local session and halts commands: sign-in can replace a server credential, so
there is no automatic SIWE refresh or retry. Session expiry stops private calls
and stream reconnects. You must explicitly decide when to establish a new session.

To enable **real order writes**, deliberately construct the client with both
permissions true, for example:

```python
config = TestnetConfig(
    allow_live_private=True,
    allow_live_orders=True,
    max_order_notional="200",
    max_position="0.010",
    max_open_orders=2,
)
```

After creating/authenticating that client, the reusable methods are:

```python
from decimal import Decimal
from bond_perpdex import Quote

market = client.market("BTCUSDCPERP")
intent = client.prepare_order(wallet, market, Quote("BUY", Decimal("64939.9"), Decimal("0.002")))
ack = client.submit_order(intent)
cancel_ack = client.cancel_order(market, ack["orderId"])
terminal = client.query_order(market.symbol, order_id=ack["orderId"])
```

The price above is illustrative, **not a recommended or current live quote**.
Use fresh market data and review testnet identity/collateral before executing.
`prepare_order()` signs but does not submit; `submit_order()` performs signed REST
position/open-order reads immediately before the one POST. It rejects stale
prepared requests, wrong registry domains, excessive notional, too many orders,
unreconciled known orders, and worst-case inventory after either side fills.
Defaults target one active market and a dedicated single-writer subaccount.
Existing orders from other processes are not safe to share with this example.
These non-atomic projection reads are conservative checks, not a margin guarantee.
Orders can fill before cancellation; a cancel acknowledgement does not undo fills.

An ordinary unrestricted single-cancel response **omits `status` in the current
source**. The SDK returns that response unchanged and records the UUID in
`pending_cancellations`; it does not synthesize `CANCELED`. New live submissions
remain blocked until `query_order(..., order_id=...)` or `reconcile_account()`
observes a terminal order status (`CANCELED`, `FILLED`, or `EXPIRED`). A temporarily
`NEW` projection is not terminal proof. These are read-side checks, never mutation
retries. Cancel-all and armed-trigger cancellation have different source response
shapes and can explicitly report `CANCELED`.

Cancellation of a specific UUID is supported even if it was not created by this
process; the server enforces authenticated account ownership. A deliberate
whole-symbol sweep is separately explicit:

```python
cancelled = client.cancel_all("BTCUSDCPERP", confirm_all_for_symbol=True)
```

This sends **DELETE `/fapi/v1/openOrders`**, with signed `symbol`, timestamp,
recvWindow and a UUID `newClientOrderId`; it does not use Binance's
`/allOpenOrders`. It cancels **all orders on that symbol for the authenticated
subaccount**, not merely this bot's orders. Call only when that scope is intended.
You may supply `client_order_id` to `cancel_order`/`cancel_all` so the command
receipt key is known before execution. No mutation is retried. Ambiguous responses
set `halted`, retain the original request under its ID in `uncertain_commands`,
and prohibit a second cancellation attempt/sweep. `query_order(symbol, client_id)`
and `reconcile_account(symbol)` provide read-side investigation without clearing
an unknown-command halt. State is in memory: do not restart to bypass that halt.

### Private WebSocket behavior and source caveats

`user_events()` uses the separately pinned
`wss://perpdex-testnet.bond.xyz/ws-fapi/v1`, performs `session.logon`, then
`userDataStream.subscribe`, and correlates each response by its string `id`.
The current source has a nonstandard logon signing contract:

```text
wire params: {apiKey, timestamp, recv_window: "5000", signature}
Ed25519 signed bytes: recvWindow=5000
```

`SessionLogonParams` deserializes the **snake_case** `recv_window`; its `to_map()`
contains only camelCase `recvWindow`. **Neither timestamp nor apiKey is signed by
this method**, although the server separately validates timestamp and the API key.
This is a source security limitation, not a recommended design. The example
reproduces it rather than inventing a more secure but incompatible signature.
Use only the exact TLS testnet endpoint, protect both session token and signatures,
and re-review this behavior if the server changes. It differs from
`userDataStream.subscribe.signature`, which is not used here.

Plain `userDataStream.subscribe` sends `params: {}` after logon and obtains
`result.subscriptionId`. Events are `{subscriptionId, event: {...}}`. The client
buffers a bounded number of events that race the subscribe acknowledgement and
validates the subscription ID after the acknowledgement. Actual event variants:

- `executionReport`: `e`, `E`, global `I`, symbol `s`, order UUID `i`, transition
  `x` (`TRADE`/`CANCELED`/`REJECTED`), status `X`, and source quantity/price fields.
- `positionUpdate`: `e`, `E`, `I`, **camelCase** `eventId`, `accountNonce`,
  `positionAmt`, `virtualQuoteBalance`, `isolatedMargin`, `reservedMargin`,
  `perpWalletQuoteBalance`, plus `symbol` and `leverage`; `user_id` is not on wire.
- `balanceUpdate`: `e`, `E`, asset `a`, decimal delta `d`, time `T`.
- `outboundAccountPosition`: the current converter emits **only**
  `{e: "outboundAccountPosition"}`. No balance snapshot is invented; reconcile REST.
- `eventStreamTerminated`: `e` and `E`; discard the subscription and reconcile.

Execution and position `I` share a process-global counter before account filtering.
A forward jump may represent other accounts' events, not proven loss on this
account. A lower/equal value may indicate restart/reset or a duplicate; it is not
silently dropped. These cases emit reconciliation-required notices. The SDK's
`UserStreamMessage` wrapper distinguishes local connection notices from original
server event dictionaries; it never invents server frames or sequence epochs.

Disconnect, idle timeout, and explicit termination permit only a bounded reconnect
(default two, hard maximum three) with capped backoff, fresh WS logon and new
subscription. There is **no resume cursor, replay, automatic REST mutation, or
automatic SIWE renewal**. RPC errors, malformed events, and expired sessions fail
closed rather than retry credentials. Known/unknown command state is never reset
by reconnect. Initial subscription, reconnect, sequence anomalies, stale events,
and stopping the iterator set `stream_requires_reconciliation`, which blocks new
live submissions until `reconcile_account()` succeeds while the stream is open.
Reconciliation never clears `halted` after an uncertain mutation. Use `closing()`
when stopping iteration early. The iterator is synchronous and single-consumer;
it is not an automatic order/fill reconciliation engine or a lossless ledger.

## Wire compatibility

The sole protocol authority was the local `bond-perpdex` implementation at:

```text
34869838ed5588f6a4686f6663e824212287ddb3
```

Source anchors below are relative to that repository, available alongside this
directory at `../bond-perpdex`. No documentation from another exchange, generic
Binance SDK, external registry, or guessed deployment was used as authority.

| Behavior | Source anchor at the pinned revision |
| --- | --- |
| SIWE message layout, wallet personal-sign, Ed25519 public PEM, `secret_type`, `secret_key` | `services/trading/client/src/client.rs::signin_with_options` (lines 353–413); `services/mm-bot/src/auth.rs` (lines 110–146) |
| `GET /auth/nonce`, `POST /auth/signin`, response `api_key`, `expires_at`, `id` | `services/trading/src/api/auth.rs`; `services/trading/src/models/auth.rs` |
| Sorted form canonicalization, null omission, URL form encoding | `services/trading/src/util/common.rs::build_query_string`; `services/trading/src/models/trading.rs::OrderRequest::to_map` |
| Nested `signedOrder` compact JSON, source browser percent-encoding vector | `services/trading/src/models/trading.rs::browser_sorted_form_golden_binds_explicit_reduce_only_values` (line 1656 onward); `services/trading/src/api/order_request.rs` |
| Legacy session `x-api-key` and base64 Ed25519 signature over canonical form | `services/trading/client/src/client.rs::ed25519_sign`, `new_perp_order_signed`, `submit_prepared_perp_command`, `post_signed` |
| Vertex schema, integer strings, hashes and signature recovery | `core/types/src/signed_intent.rs::VertexEip712Domain`, `VertexOrder`, `SignedOrderEnvelope`; `services/settlement/tests/fixtures/signed_intent_vectors.json` |
| POST_ONLY expiration bits and low-s ECDSA | `tools/loadgen/src/sign.rs::SignedOrderKind`, `sign_vertex_order`; `core/orderbook-rs/src/orders/provenance.rs::VertexOrderSemantics` |
| Authenticated sender, canonical chain/book, contract time and size-increment checks | `services/trading/src/service/engine/order.rs::validate_signed_order_context` (line 436); `authenticated_subaccount` (line 486) |
| POST_ONLY requires GTC; LIMIT+GTX normalization | `services/trading/src/models/trading.rs`; `services/trading/src/handler/trading.rs::new_order` |
| POST and form DELETE `/fapi/v1/order` | `services/trading/src/api/perp/trading.rs`; `CancelOrderRequest::to_map` in `models/trading.rs` |
| Ordinary single-cancel status omission versus cancel-all terminal status | `services/trading/src/service/engine/order.rs::build_cancel_order_outcome` (around line 3424); `build_cancel_open_orders_outcome` (around line 3775) |
| Signed `openOrders` and `positionRisk` reads | `services/trading/src/api/perp/account.rs`; `services/trading/src/models/account.rs::QueryOpenOrdersRequest`, `PositionInformationParams` |
| Cancel-all form fields and route | `services/trading/src/models/trading.rs::CancelOpenOrdersRequest`; `services/trading/src/api/perp/trading.rs::router` |
| Private WS logon signing asymmetry and timestamp verification | `services/trading/src/models/session.rs::SessionLogonParams`; `services/trading/src/handler/ws.rs::process_session_logon`; `services/trading/src/util/security.rs::validate_request` |
| Private subscription response, event fields, and sparse account converter | `services/trading/src/handler/ws.rs::process_data_stream_subscribe`; `services/trading/src/models/ws.rs` |
| Private global sequence allocation, best-effort delivery and termination | `services/trading/src/service/execution_report.rs::emit`; `services/trading/src/service/user_data_stream.rs::route_events`, `terminate_subscription` |
| Market IDs, precision, tick/step, minimum notional | `core/types/src/symbol.rs` (BTC starts at line 278); `services/market-data/src/api.rs` |
| WS SUBSCRIBE/ack/envelope and depth sequence fields | `services/market-data/src/wss_api.rs::a_depth_subscription_acknowledges_and_relays_real_frames`; `services/market-data/core/src/models/wss.rs::DiffBookDepthResponse` |
| Absolute wire depth quantities, zero removals, `U/u/pu` gaps | `services/market-data/core/src/handler/depth.rs::DepthPendingPublish::into_diff_book_depth_response`, `Depth::update_depth`; `services/market-data/src/wss_api.rs::the_real_publisher_delivers_depth_frames_to_a_subscribed_socket` |
| Testnet endpoint, chain and current VirtualBooks | `deploy/environments/staging-release/runtime-registry.json` |
| Existing MM lifecycle, unknown-result and restart concerns | `services/mm-bot/src/bot.rs`, `journal.rs`, `signed_order.rs`, `continuity.md` |

The packaged `tests/fixtures/order-vector.json` is the **order** object extracted
unchanged from the source golden fixture. Its `0x1111…` address is an explicitly
synthetic source test domain, never a deployment address. `signed-order-form.txt`
is the exact source Rust browser-encoding literal. Source fixture provenance:
ethers.js 5.7.2, reviewed Vertex contracts at
`7ae12f1605e8d3c0790fdfbb98922b6014b00377`. No private key was copied.

The implemented signing sequence is:

1. Fetch the SIWE nonce, personal-sign the EIP-4361 message
   with secp256k1/EIP-191, binding the generated Ed25519 public key in its statement.
   The response establishes a session. SIWE nonce is **not** an order nonce.
2. Encode `sender = owner_address || subaccount_name_right_padded_to_12_bytes`.
   This example intentionally accepts only 1–12 ASCII alphanumeric name bytes.
3. Sign `Order(bytes32 sender,int128 priceX18,int128 amount,uint64 expiration,uint64 nonce)`
   with EIP-712 domain `Vertex`, `0.0.1`, chain 16602 and the registry VirtualBook.
   Price uses exact X9 then X18 conversion; base quantity uses precision 8 and X18.
   Sells have negative signed amount. Integers in the envelope are decimal strings.
   POST_ONLY/GTC sets `expiration = (3 << 62) | expiry_seconds`; no reduce-only bit.
   Signatures are low-s secp256k1, 65 bytes, `v` 27/28. All four hash evidence fields
   are transported.
4. Put that envelope in compact `signedOrder` JSON. Sort request field names and
   URL-form-encode, then Ed25519-sign those exact bytes and append base64
   `signature`. Send `x-api-key`, not Binance HMAC or the separate `x-bond-*`
   user-generated API-key protocol. Cancellation is a signed **form DELETE**, not
   JSON. Request timestamps are milliseconds; envelope expiry is seconds.

BTC fixture filters are sourced: tick `0.10`, step/min quantity `0.001`, base
precision 8, minimum notional 100. Quotes use `0.002` BTC so both exceed the minimum
at the illustrative 65,000 price. Prices and quantities are synthetic market data,
not observed testnet prices. Filters are parsed from `exchangeInfo` and exact
decimals are validated before signing; no float arithmetic or silent rounding of
caller-supplied orders. Strategy rounding goes outward to the tick.

## Safety and deliberately unsupported behavior

- **Freshness and disconnects:** a snapshot alone is insufficient to quote. A
  contiguous depth event must bridge it; subsequent `pu` values must match. Stale,
  future-dated, malformed, wrong-symbol, crossed, or gapped data clears the book.
  Duplicates do not renew freshness. No automatic reconnect or stale-book reuse.
  A future online strategy must subscribe/buffer before REST snapshot, resync on
  every gap, and monitor venue health; the optional raw reader does not do this.
- **Inventory and cancellation:** one quote per permitted side, a hard position
  bound under either side filling, a per-order notional cap, and fresh inventory
  are required. Outstanding quotes block replacement. The demo checks private
  position/open-order reads, cancels only its acknowledged orders on shutdown,
  attempts all known cancellations even if one fails, and confirms an empty book.
  An external caller must feed accurate inventory and outstanding-order state to
  `MakerPolicy`; it is not an account-wide risk manager.
- **Unknown outcomes:** timeouts, ambiguous acknowledgements, or unconfirmed
  cancellations halt submission with `UnknownOutcome`. There is no retry, replay,
  automatic new client ID, or assumption that a failed HTTP request did nothing.
  Unknown cancellation retains the possibly-open order internally and cannot be
  retried. Inspect the original intent and venue state. The source has command
  receipts and richer reconciliation; this package does **not** claim that an
  empty eventual-consistency read proves an unknown submission never happened.
- **Nonces and restart:** real order-building defaults use random nonzero uint64
  nonces with per-client reuse checks. The deterministic demo uses 42 and 43 only
  in a fresh in-memory venue. Order nonces are distinct from SIWE challenges,
  timestamps, and the engine's account/position nonces. No sequential “next order
  nonce” endpoint is invented. Client IDs are UUIDs. State is deliberately not
  durable: this cannot safely resume trading after restart or coordinate multiple
  processes. Real methods are for explicitly supervised testnet integration, not
  unattended market making.
- **No inferred capabilities:** the source supports other order types, amend,
  private WS order commands, transfers, withdrawals, and a newer user-generated
  API-key scheme; none is implemented here. Private WS session/subscription is
  implemented, but orders use REST only. POST_ONLY/GTC is not reduce-only. No margin,
  liquidation, funding, fees, settlement, partial-fill engine, or collateral
  provisioning is simulated. The mocks validate signatures and
  wire terms but does not certify venue admission or economic execution.
- **Deployment uncertainty:** the pinned registry supersedes the older MM fallback
  VirtualBook table. It describes source configuration, not independently observed
  current deployment identity. The server additionally reads Endpoint time and
  on-chain product size increments during signed ingress; `exchangeInfo` alone
  does not certify these. Development performed no RPC/deployment/account access
  and cannot attest current venue availability, balances, margin, contract size
  increments, or SIWE policy. Re-review the registry and these gates before any
  future explicitly approved live integration. Source signed terms and wire
  encodings are verified; current runtime state remains intentionally unverified.

## Checks

```sh
.venv/bin/ruff format --check src tests
.venv/bin/ruff check src tests
.venv/bin/pytest -q
.venv/bin/pytest -q tests/test_smoke.py
.venv/bin/python -m bond_perpdex
.venv/bin/python -m pip check
```

Tests forbid sockets and DNS at the fixture boundary. They exercise source
EIP-712 hashes/recovery and exact form bytes, both signature layers, post-only
flags, decimal precision, nonce bounds/reuse, testnet and credential guards,
HTTP/WS transport mocks, redirects/errors without retries, depth freshness/gaps,
inventory limits, lost submit/cancel acknowledgements, session expiry, and cleanup.
Real HTTP auth/account/place/cancel/cancel-all paths are exercised through
`httpx.MockTransport`, and private WS session/signature/events/reconnect paths
through bounded socket doubles. No live request is used to validate either path.
The no-transaction smoke invokes the actual CLI with socket access forbidden.
It is deterministic, needs no secrets, and cannot place a live order.

No source-repository files were modified or built. No Git repository was initialized,
no commits/branches/pushes were made, and no GitHub repository was created. This
local deliverable awaits **Red's approval of initial repository creation and
visibility**; publication and a license choice are intentionally left to that review.
