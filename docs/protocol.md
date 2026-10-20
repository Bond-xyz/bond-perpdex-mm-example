# Protocol and integration reference

Start with the [README](../README.md) for installation and the
[OEMS integration guide](oems-integration.md) for direct answers to signing,
compatibility, authentication, and rate-limit questions. This reference covers
real testnet usage, protocol details, and synthetic signing vectors.

## Public read-only depth

From the project directory, run:

```sh
make public
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
[verification summary](galileo-live-verification.md) for checked flows and limits;
fill/fee behavior and sustained throughput remain untested.
Use a separately provisioned, dedicated testnet wallet/subaccount.
Inject `BOND_TESTNET_WALLET_KEY` (32-byte hex) through your secret manager into the
process environment. These direct Python snippets do not load `.env`; the
[quick-start runner](configuration.md) supports an owner-only `.env` instead.
Do not paste a key into shell commands, source files, notebooks or chat. Retain the
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
from bond_perpdex_client import Quote, read_endpoint_time

market = client.market("BTCUSDCPERP")
endpoint_time = read_endpoint_time(verified_galileo_rpc_url, client.signing_registry)
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

An ordinary single-cancel response **can omit `status`**. The sample client
returns that response unchanged and records the UUID in
`pending_cancellations`; it does not synthesize `CANCELED`. New live submissions
remain blocked until `query_order(..., order_id=...)` or `reconcile_account()`
observes a terminal order status (`CANCELED`, `FILLED`, or `EXPIRED`). A temporarily
`NEW` projection is not terminal proof. These are read-side checks, never mutation
retries. A cancel-all response can explicitly report `CANCELED`.

Cancellation of a specific UUID is supported even if it was not created by this
process; the server enforces authenticated account ownership. A deliberate
whole-symbol sweep is separately explicit:

```python
cancelled = client.cancel_all("BTCUSDCPERP", confirm_all_for_symbol=True)
```

On live Galileo this sends signed WebSocket **`openOrders.cancelAll`**, with
`symbol`, timestamp, recvWindow, UUID `newClientOrderId`, signature and SIWE
`apiKey`. Public REST `DELETE /fapi/v1/openOrders` currently returns 405 on
Galileo. There is no REST fallback.
The offline fixture retains the backend form semantics without network access.
Cancel-all affects every order on that symbol in the authenticated subaccount.
You may supply `client_order_id` to `cancel_order`/`cancel_all` so the command
receipt key is known before execution. No mutation is retried. Ambiguous responses
set `halted`, retain the original request under its ID in `uncertain_commands`,
and prohibit a second cancellation attempt/sweep. `query_order(symbol, client_id)`
and `reconcile_account(symbol)` provide read-side investigation without clearing
an unknown-command halt. State is in memory: do not restart to bypass that halt.

### Private WebSocket behavior

`user_events()` connects to the exact trading endpoint
`wss://perpdex-testnet.bond.xyz/ws-fapi/v1` and sends
`userDataStream.subscribe.signature`. Ed25519 signs sorted
`recvWindow=5000&timestamp=<Unix-ms>`; `apiKey` and `signature` are excluded.
No session.logon or unsigned subscribe is used. Each acknowledgement is correlated
by its string `id`; the result supplies `subscriptionId`. Events instead have
`{subscriptionId, event: {...}}`. The client buffers a bounded number of events
that race the acknowledgement and validates their subscription ID. Event variants:

- `executionReport`: `e`, `E`, global `I`, symbol `s`, order UUID `i`, transition
  `x` (`NEW`/`TRADE`/`AMENDMENT`/`CANCELED`/`EXPIRED`/`REJECTED`), status `X`,
  and source quantity/price fields. `NEW` confirms an accepted order before any
  fill it takes; `AMENDMENT` carries an in-place price/quantity change with the
  unchanged status; `EXPIRED` ends an IOC/FOK order with quantity left unfilled,
  after any fills it took. Venues that predate these values never send them.
- `positionUpdate`: `e`, `E`, `I`, **camelCase** `eventId`, `accountNonce`,
  `positionAmt`, `virtualQuoteBalance`, `isolatedMargin`, `reservedMargin`,
  `perpWalletQuoteBalance`, plus `symbol` and `leverage`; `user_id` is not on wire.
- `balanceUpdate`: `e`, `E`, asset `a`, decimal delta `d`, time `T`.
- `outboundAccountPosition`: the current converter emits **only**
  `{e: "outboundAccountPosition"}`. No balance snapshot is invented; reconcile REST.
- `eventStreamTerminated`: `e` and `E`; discard the subscription and reconcile.

Execution and position `I` values are not a contiguous per-account sequence.
A forward jump alone does not prove loss on this account. A lower/equal value may
indicate restart/reset or a duplicate; it is not silently dropped. These cases
emit reconciliation-required notices. The sample client's
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

Public `GET /fapi/v1/exchangeInfo` exposes `chainId`, `endpoint`, and per-symbol
`productId`/`virtualBook` from the service's validated release binding.
`client.market(symbol)` validates this metadata and live filters; no private
registry is required. `make discover` performs credential-free validation.
Optional file pins must match discovery. Missing fields, invalid mappings or a
rotation during a run stop trading; reconcile before using a new client.
Deploy the exchangeInfo extension before running discovery-based trade mode.

`tests/fixtures/order-vector.json` and `signed-order-form.txt` check EIP-712
hashing/signature recovery and exact form encoding. Their addresses and signing
identities are public, synthetic test inputs, never funded-account credentials.

The implemented signing sequence is:

1. Fetch the SIWE nonce, personal-sign the EIP-4361 message
   with secp256k1/EIP-191, binding the generated Ed25519 public key in its statement.
   The response establishes a session. SIWE nonce is **not** an order nonce.
2. Encode `sender = owner_address || subaccount_name_right_padded_to_12_bytes`.
   This example intentionally accepts only 1–12 printable ASCII name bytes without spaces.
3. Sign `Order(bytes32 sender,int128 priceX18,int128 amount,uint64 expiration,uint64 nonce)`
   with EIP-712 domain `Vertex`, `0.0.1`, chain 16602 and the publicly discovered
   market VirtualBook. There are no hardcoded live signing-address defaults. Use `read_endpoint_time` before live order preparation.
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
  retried. Inspect the original intent and venue state. An empty
  eventual-consistency read does not prove an unknown submission never happened.
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
  public signing context and filters from exchangeInfo, then read Endpoint time.
  VirtualBooks may be signing-domain addresses without runtime code. Schema and
  offline vector checks do not establish funded trading acceptance; see the
  dated [verification record](oems-integration.md#verification).

## Checks

```sh
make check
make test ARGS=tests/test_smoke.py
make offline
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
