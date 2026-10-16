# Protocol and integration reference

Start with the [README](../README.md) for installation and the
[OEMS integration guide](oems-integration.md) for direct answers to signing,
compatibility, authentication, and rate-limit questions. This reference covers
real testnet usage, protocol details, and source evidence.

## Public read-only depth

From the project directory, run:

```sh
.venv/bin/python -m bond_perpdex_client --testnet-read-only
```

This makes real public testnet requests: it fetches `exchangeInfo` and a depth
snapshot, receives one acknowledged WebSocket depth event, and exits. It requires
no credentials and sends no orders. It does not need a signing registry because
it does not select an EIP-712 domain. Public health/market/status reads returned
200 on 16 October 2026. HTTP/WS venue URLs remain exact allowlists; redirects and
environment proxies are disabled, with bounded timeouts and message sizes.
HTTPS and WSS use certificate/hostname verification and the HTTP client's trusted
CA roots, including on Python installations without system CA certificates.

## Transport permissions

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

## Explicitly enable the sample client's testnet access

**These snippets make real requests if you run them.** Use the current
[verification notes](oems-integration.md#verification) for observed behavior;
funded order acceptance has not been established.
Use a separately provisioned, dedicated testnet wallet/subaccount.
Inject `BOND_TESTNET_WALLET_KEY` (32-byte hex) through your secret manager into the
process environment. Do not paste a key into a shell command, configuration file,
source file, notebook, or chat. No `.env` loading is performed. Retain the
Ed25519 key in a separate owner-only PEM; an optional owner-only session cache
stores the token, expiry and public identity, never the private key. Python
cannot promise secure zeroization. Never enable debug wire logging.

Authentication and private reads/notifications are independently enabled from
order mutations:

```python
from contextlib import closing

from bond_perpdex_client import (BondPerpDexClient, TestnetConfig, TestnetTransport,
                                 WalletSigner, load_ed25519_private_key)

config = TestnetConfig(allow_live_private=True, allow_live_orders=False)
client = BondPerpDexClient(TestnetTransport(config))
wallet = WalletSigner.from_environment()
key = load_ed25519_private_key("/secure/galileo-ed25519.pem")
client.authenticate_cached(wallet, key, "tradegods-z", "/secure/galileo.session.json")
snapshot = client.reconcile_account("BTCUSDCPERP")

with closing(client.user_events(max_events=20, max_reconnects=2)) as notifications:
    for message in notifications:
        if message.requires_reconciliation and message.kind != "disconnected":
            snapshot = client.reconcile_account("BTCUSDCPERP")
```

`authenticate()` requires an explicit live name and signing key, then performs
`GET /auth/nonce` and `POST /auth/signin`. An ambiguous sign-in clears the old
local session and halts commands; there is no retry. `authenticate_cached()`
reuses a matching, validated token or renews explicitly when 24 hours or less
remain. Reader failure does not replace credentials. Session expiry stops private
calls and stream reconnects. Coordinate one renewal owner; see the OEMS guide.

To enable **real order writes**, deliberately construct the client with both
permissions true, for example:

```python
from bond_perpdex_client import SigningRegistry

config = TestnetConfig(
    allow_live_private=True,
    allow_live_orders=True,
    signing_registry=SigningRegistry.from_file("/secure/perpdex.local.json"),
    max_order_notional="200",
    max_position="0.010",
    max_open_orders=2,
)
```

After creating/authenticating that client, the reusable methods are:

```python
from decimal import Decimal
from bond_perpdex_client import Quote, read_endpoint_time

market = client.market("BTCUSDCPERP")
endpoint_time = read_endpoint_time(verified_galileo_rpc_url, config.signing_registry)
intent = client.prepare_order(wallet, market,
    Quote("BUY", operator_price, operator_quantity), endpoint_time=endpoint_time)
ack = client.submit_order(intent)
cancel_ack = client.cancel_order(market, ack["orderId"])
terminal = client.query_order(market.symbol, order_id=ack["orderId"])
```

Supply operator-selected Decimal price/quantity and a verified Galileo RPC URL.
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
source**. The sample client returns that response unchanged and records the UUID in
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

On live Galileo this sends signed WebSocket **`openOrders.cancelAll`**, with
`symbol`, timestamp, recvWindow, UUID `newClientOrderId`, signature and SIWE
`apiKey`. The backend REST `DELETE /fapi/v1/openOrders` exists, but the public
gateway routes it to a GET-only reader and returns 405. There is no REST fallback.
The offline fixture retains the backend form semantics without network access.
Cancel-all affects every order on that symbol in the authenticated subaccount.
You may supply `client_order_id` to `cancel_order`/`cancel_all` so the command
receipt key is known before execution. No mutation is retried. Ambiguous responses
set `halted`, retain the original request under its ID in `uncertain_commands`,
and prohibit a second cancellation attempt/sweep. `query_order(symbol, client_id)`
and `reconcile_account(symbol)` provide read-side investigation without clearing
an unknown-command halt. State is in memory: do not restart to bypass that halt.

### Private WebSocket behavior and source caveats

`user_events()` connects to the exact trading endpoint
`wss://perpdex-testnet.bond.xyz/ws-fapi/v1` and sends
`userDataStream.subscribe.signature`. Ed25519 signs sorted
`recvWindow=5000&timestamp=<Unix-ms>`; `apiKey` and `signature` are excluded.
No session.logon or unsigned subscribe is used. Each acknowledgement is correlated
by its string `id`; the result supplies `subscriptionId`. Events instead have
`{subscriptionId, event: {...}}`. The client buffers a bounded number of events
that race the acknowledgement and validates their subscription ID. Event variants:

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
silently dropped. These cases emit reconciliation-required notices. The sample client's
`UserStreamMessage` wrapper distinguishes local connection notices from original
server event dictionaries; it never invents server frames or sequence epochs.

Disconnect, idle timeout, and explicit termination permit only a bounded reconnect
(default two, hard maximum three) with capped backoff, a fresh signed
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

The signing and wallet-session implementation was pinned to the local
`bond-perpdex` implementation at:

```text
df07bef07f7e6098861fae2a8435132aadff11a4
```

Source anchors below are relative to `Bond-xyz/bond-perpdex` at that revision.
The canonical signing-address authority is `Bond-xyz/bond-shared-registry`;
operators must verify its Galileo manifest against the active release. Source
signing fixtures are synthetic and never select live addresses.

| Behavior | Source anchor at the pinned revision |
| --- | --- |
| SIWE message layout, wallet personal-sign, Ed25519 public PEM, `secret_type`, `secret_key` | `services/trading/client/src/client.rs::signin_with_options`; `services/mm-bot/src/auth.rs` |
| `GET /auth/nonce`, `POST /auth/signin`, response `api_key`, `expires_at`, `id` | `services/trading/src/api/auth.rs`; `services/trading/src/models/auth.rs` |
| Sorted form canonicalization, null omission, URL form encoding | `services/trading/src/util/common.rs::build_query_string`; `services/trading/src/models/trading.rs::OrderRequest::to_map` |
| Nested `signedOrder` compact JSON, source browser percent-encoding vector | `services/trading/src/models/trading.rs::browser_sorted_form_golden_binds_explicit_reduce_only_values`; `services/trading/src/api/order_request.rs` |
| Legacy session `x-api-key` and base64 Ed25519 signature over canonical form | `services/trading/client/src/client.rs::ed25519_sign`, `new_perp_order_signed`, `submit_prepared_perp_command`, `post_signed` |
| Vertex schema, integer strings, hashes and signature recovery | `core/types/src/signed_intent.rs::VertexEip712Domain`, `VertexOrder`, `SignedOrderEnvelope`; `services/settlement/tests/fixtures/signed_intent_vectors.json` |
| POST_ONLY expiration bits and low-s ECDSA | `tools/loadgen/src/sign.rs::SignedOrderKind`, `sign_vertex_order`; `core/orderbook-rs/src/orders/provenance.rs::VertexOrderSemantics` |
| Authenticated sender, canonical chain/book, contract time and size-increment checks | `services/trading/src/service/engine/order.rs::validate_signed_order_context`; `authenticated_subaccount` |
| POST_ONLY requires GTC; LIMIT+GTX normalization | `services/trading/src/models/trading.rs`; `services/trading/src/handler/trading.rs::new_order` |
| POST and form DELETE `/fapi/v1/order` | `services/trading/src/api/perp/trading.rs`; `CancelOrderRequest::to_map` in `models/trading.rs` |
| Ordinary single-cancel status omission versus cancel-all terminal status | `services/trading/src/service/engine/order.rs::build_cancel_order_outcome`; `build_cancel_all_outcome` |
| Signed `openOrders` and `positionRisk` reads | `services/trading/src/api/perp/account.rs`; `services/trading/src/models/account.rs::QueryOpenOrdersRequest`, `PositionInformationParams` |
| Cancel-all form fields and route | `services/trading/src/models/trading.rs::CancelOpenOrdersRequest`; `services/trading/src/api/perp/trading.rs::router` |
| Private signed subscription and timestamp verification | `services/trading/src/models/ws.rs`; `services/trading/src/handler/ws.rs`; `services/trading/src/util/security.rs::validate_request` |
| Private subscription response, event fields, and sparse account converter | `services/trading/src/handler/ws.rs::process_data_stream_subscribe`; `services/trading/src/models/ws.rs` |
| Private global sequence allocation, best-effort delivery and termination | `services/trading/src/service/execution_report.rs::emit`; `services/trading/src/service/user_data_stream.rs::route_events`, `terminate_subscription` |
| Market IDs, precision, tick/step, minimum notional | `core/types/src/symbol.rs` (BTC starts at line 278); `services/market-data/src/api.rs` |
| WS SUBSCRIBE/ack/envelope and depth sequence fields | `services/market-data/src/wss_api.rs::a_depth_subscription_acknowledges_and_relays_real_frames`; `services/market-data/core/src/models/wss.rs::DiffBookDepthResponse` |
| Absolute wire depth quantities, zero removals, `U/u/pu` gaps | `services/market-data/core/src/handler/depth.rs::DepthPendingPublish::into_diff_book_depth_response`, `Depth::update_depth`; `services/market-data/src/wss_api.rs::the_real_publisher_delivers_depth_frames_to_a_subscribed_socket` |
| Testnet endpoint, chain and current VirtualBooks | `platform/bond-config/src/generated.rs`; the active release registry binding and `bond-shared-registry/envs/og-testnet-staging/perpdex.json` |
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
   This example intentionally accepts only 1–12 printable ASCII name bytes without spaces.
3. Sign `Order(bytes32 sender,int128 priceX18,int128 amount,uint64 expiration,uint64 nonce)`
   with EIP-712 domain `Vertex`, `0.0.1`, chain 16602 and the operator-verified registry VirtualBook. Live callers load
   `SigningRegistry.from_file` and supply it in `TestnetConfig`; there are no live
   signing-address defaults. Use `read_endpoint_time` before live order preparation.
   Price uses exact X9 then X18 conversion; base quantity uses precision 8 and X18.
   Sells have negative signed amount. Integers in the envelope are decimal strings.
   POST_ONLY/GTC sets `expiration = (3 << 62) | expiry_seconds`; no reduce-only bit.
   Use `expiry_seconds = max(client_wall_time, endpoint_time) + 60`. Ingress also
   checks wall time, and Endpoint time can lag between settlement batches.
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
  Duplicates do not renew freshness. No automatic market-book reconnect or stale-book reuse.
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
  nonce” endpoint is invented. Client IDs are UUIDs. Command state is not
  durable: a session cache does not restore command state or safely resume trading
  after restart or coordinate multiple processes. Real methods are for explicitly supervised testnet integration, not
  unattended market making.
- **Supported surface:** signed REST placement/cancel-one, signed private
  subscriptions and WebSocket cancel-all are implemented. A side-effect-free
  helper builds order.place frames. Other order types, amendments, deposits,
  transfers and withdrawals are outside this example. POST_ONLY/GTC is not
  reduce-only. The offline venue does not simulate matching, margin, liquidation,
  funding, fees or settlement.
- **Deployment identity:** no historical address table is a live fallback. Load
  the operator-verified manifest, refresh live filters and read Endpoint time.
  VirtualBooks may be signing-domain addresses without runtime code. Schema and
  offline vector checks do not establish funded trading acceptance; see the
  dated [verification record](oems-integration.md#verification).

## Checks

```sh
.venv/bin/ruff format --check src tests examples
.venv/bin/ruff check src tests examples
.venv/bin/pytest -q
.venv/bin/pytest -q tests/test_smoke.py
.venv/bin/python -m bond_perpdex_client
.venv/bin/python -m pip check
```

Tests forbid sockets and DNS at the fixture boundary. They exercise source
EIP-712 hashes/recovery and exact form bytes, both signature layers, post-only
flags, decimal precision, nonce bounds/reuse, testnet and credential guards,
HTTP/WS transport mocks, redirects/errors without retries, depth freshness/gaps,
inventory limits, lost submit/cancel acknowledgements, session expiry, and cleanup.
HTTP authentication/account/place/cancel-one and WebSocket subscription/cancel-all
paths are exercised through bounded mock transports. Credential and registry tests
cover identity mismatch, renewal boundaries, file permissions and missing context.
See the dated [verification record](oems-integration.md#verification) for live
checks. No funded order or fee behavior is inferred from mocks.
