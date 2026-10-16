# Galileo OEMS integration guide

These six integration answers describe Galileo behavior checked on
**16 October 2026**. See the [verification summary](galileo-live-verification.md)
for the scope of those checks.

See the [configuration quick start](configuration.md) for editable account, session,
venue, strategy and risk defaults, and the [protocol reference](protocol.md) for signing details.

## 1. Which account does the website fund, and how do makers create their own?

`https://testnet.bond.xyz/trade` enables trading and deposits into **`bond`**.
This is a configured subaccount name, not a shared custodial wallet: the identity
is **owner wallet + name**. Connecting a wallet alone does not create a session;
use Enable trading. Names are unique within a wallet, with no global username
reservation. Another wallet can use the same name.

For a dedicated maker account, choose `tradegods-z` (11 bytes). Supported names
are 1–12 printable ASCII bytes without spaces or nulls. SIWE's **Request ID**
selects the name. Example 02/03 and live `authenticate()` require an explicit
name and persistent signing key. The deterministic offline fixture may use `bond`.

`GET /auth/me` with the SIWE token in `x-api-key` returns backend UUID `id` and
zero-padded 12-byte hex `name` (`client.identity()` in Python). The on-chain sender is different: 20 wallet bytes
followed by the 12-byte name, forming a bytes32. The bytes32 is used by the Vertex
order/Endpoint flow; the UUID is used by backend APIs. Re-signing the same
wallet/name reuses the account and replaces its session/signing registration.

A new name does not inherit website funds. Fund that exact name from its owner
wallet through the verified Endpoint's `depositCollateral(bytes12,uint32,uint128)`,
after collateral-token approval: right-zero-padded name, collateral product `0`,
USDC.e amount in six-decimal base units. Wait for finalized deposit processing
and confirm available perpetual margin. The website has no named-account deposit
selector. Public spot/perp transfers are disabled on the inspected deployment;
do not assume the subaccount-transfer route moves website perpetual margin into
a new maker account. This repository performs no deposits.

## 2. Which WebSocket authentication method should I use?

Use **`userDataStream.subscribe.signature`** for private events. Do not use
`session.logon` in a new integration against this release: it has legacy signing
behavior, and its presence in the method inventory is not a recommendation.
The Python stream now sends one request of this shape:

```json
{
  "id": "subscribe-1",
  "method": "userDataStream.subscribe.signature",
  "params": {
    "apiKey": "<SIWE api_key>",
    "recvWindow": 5000,
    "timestamp": 1792137600000,
    "signature": "<base64 Ed25519 signature over sorted recvWindow and timestamp>"
  }
}
```

The signature covers `recvWindow=5000&timestamp=1792137600000`; it excludes
`apiKey` and `signature`. A successful response is
`{"id":"subscribe-1","status":200,"result":{"subscriptionId":1}}`.
Notifications instead use `{"subscriptionId":1,"event":{...}}`.

Every trading command needs its own SIWE `apiKey`, timestamp, Ed25519 signature
and command fields. Subscription does not authorize unsigned commands. Order
placement additionally carries the wallet-signed Vertex EIP-712 `signedOrder`
envelope. REST takes its compact JSON form value; WebSocket takes an object
while the account signature still covers that same compact form value.

Example 02 sends orders/cancel-one over REST. `websocket_order_place(intent)`
builds the equivalent signed frame without sending it; do not submit both
representations of an intent. `cancel_all()` sends its signed WebSocket command.
The stream is bounded and never replays orders during reconnect.

Private `outboundAccountPosition` is a sparse reconciliation signal, not a
Binance balance array. `balanceUpdate.d` is a balance delta; an execution fee is
`executionReport.n` with asset `N`. The inspected engine represents fees as
nonnegative `USDC.e` charges; no negative maker rebate is demonstrated here.
Events are best effort. Reconcile REST balances, positions, orders and fills
after gaps; the example does not certify fill/fee behavior.

## 3. Can I discover product IDs and VirtualBooks over REST?

Use unauthenticated **`GET /fapi/v1/exchangeInfo`**. Its public signing extension
returns `chainId` and `endpoint` at the top level, and `productId` (integer) and
`virtualBook` (EVM address) on every perpetual symbol, alongside precision and
trading filters. These values come from the service's validated deployment
binding; no private repository access is required.

`make discover` validates and prints this context without credentials or orders.
`client.market(symbol)` discovers it automatically for live trading. The client
requires chain 16602, nonzero addresses, distinct product IDs/VirtualBooks and
unambiguous symbols. An optional `SigningRegistry.from_file(path)` pin must match
the public response. Missing fields or a changed mapping stop trading; never
substitute historical addresses or offline fixtures. A new client after a
rotation must first reconcile outstanding effects.

**Rollout requirement:** deploy the market-data exchangeInfo extension before
using discovery-based trade mode. Older servers omit these fields and are
rejected. Existing private account reads do not require the extension.

Read the discovered Endpoint's `getTime()` through a verified chain-16602 RPC
before signing (`read_endpoint_time`). Expiry also accounts for client wall time.
A VirtualBook may be an EIP-712 domain address without runtime code; do not replace
it with another contract merely because it has bytecode.

## 4. Is there a REST cancel-all endpoint?

The backend implements **`DELETE /fapi/v1/openOrders`**, form-encoded with
`symbol`, `newClientOrderId`, `timestamp`, optional `recvWindow`, and Ed25519
`signature`. It affects all orders for **one symbol in one authenticated
subaccount**, not every symbol/account. Managed keys additionally require the
appropriate `trade:write` scope and signed `x-bond-*` headers.

**Current deployment caveat:** Galileo's public REST request returns **405**;
REST cancel-all is currently unavailable there. `cancel_all()` therefore uses
signed **`openOrders.cancelAll`** on the trading WebSocket, with the same signed
fields plus SIWE `apiKey`. It never falls
back to REST or replays on socket failure. The bounded example opens one command
socket per sweep; an OEMS should reuse a correlated command connection and bound
outstanding work. Repeat explicitly per symbol for multiple markets.

Both cancel-one and cancel-all require `newClientOrderId`: 1–36 characters from
`[A-Za-z0-9._:-]`. This client uses UUIDs and lets the caller supply one before
execution. Retain it for reconciliation of that exact intent; a later bulk sweep
requires a fresh key. This client does not automatically retry a cancellation.
Individual signed REST `DELETE /fapi/v1/order` is still an alternative. REST
cancel acknowledgements can omit status; reconcile terminal state before replacing
an order. A timeout is an unknown outcome, not proof that nothing happened.

## 5. Which rate limits apply?

| Layer | Inspected Galileo behavior |
| --- | --- |
| SIWE `x-api-key` REST | The managed-key 120-request counter does not apply. Separate per-IP admission protection still applies; this guide does not promise a per-session throughput allocation. Bound request rates and honor 429/Retry-After. |
| Managed `x-bond-api-key` REST | 120 requests per rolling 60 seconds per key on supported managed-key trading routes. A 429 includes `Retry-After`. Current public account-query routes require SIWE. |
| Trading WebSocket commands | No fixed per-second/per-minute counter is implemented for `order.place`, `order.cancel` or `openOrders.cancelAll`. Processing deadlines, capacity, admission and backpressure remain binding. |
| Shared REST/WS counter | None for trading commands. HTTP admission and WebSocket connection limits still apply separately. |
| WebSocket upgrades | 300 new connections per rolling 5 minutes per IP per service process. Public market-data connections have a separate budget. This is a connection limit, not a command allowance. |
| Public market messages | Five incoming messages per second; this is not a trading-command limit. |
| Optional WS `rate_limits` | A compatibility field describing `rateLimitType`, `interval`, `intervalNum`, `limit` and `count`. Current handlers omit it; `returnRateLimits` does not activate live quota reporting. |

`exchangeInfo.rateLimits` is separate advertised metadata. Its REQUEST_WEIGHT
and ORDERS entries are not independently enforced command counters. Self-meter,
bound outstanding commands, honor HTTP 429/Retry-After, and back off on socket
errors. Do not infer unlimited throughput or test capacity by flooding the venue.

## 6. Which credentials should a long-running OEMS use?

Use a dedicated named subaccount with a **SIWE wallet session and persistent
Ed25519 key** for trading/private WebSocket and REST reconciliation. Provisioned
`x-bond-api-key` keys remain REST-only; there is no supported managed-key private
WebSocket authentication shape. Public account-query routes also require SIWE.
Example 04 is consequently an **offline header-shape example for a trading REST
cancellation**, not a bot-key account read against the current deployment.

Both credential families can be reused across process restarts while valid
and with their matching signing key retained. A managed key expires, is revoked
or rotates through key management; repeating SIWE sign-in does not renew it.
The managed REST signature has six newline-separated lines:
`timestamp\nnonce\nMETHOD\npathAndQuery\nsubaccountOrDefault\nsha256hex(rawBody)`.
Its nonce is client-generated and single-use per key. Omitted account headers
sign literal `default`, selecting the key owner; UI-created dedicated accounts
should explicitly supply their authorized UUID. Key-management calls use SIWE
and a **server-issued** `/auth/nonce` challenge with a five-line signature instead.
This package prepares managed headers but does not create or rotate managed keys.

For SIWE, the server's returned `expires_at` is authoritative; inspected sessions
last **30 days**. `authenticate_cached(wallet, key, name, path)` optionally stores
the token, UUID, expiry, venue, wallet/name and public-key identity atomically in
an owner-only file. The private key stays in its separate owner-only PEM. A valid
matching session is validated with `/auth/me` and reused. An identity mismatch or
reader failure never triggers silent credential replacement. Without a cache,
each explicit sign-in replaces the previous token and can replace its signing key.

Renew **24 hours before expiry**: coordinate one renewal owner, pause submissions,
reconcile pending/uncertain effects, close the old stream, call the renewal helper
with the same wallet/name and persistent key, save the replacement token, open a
new socket, subscribe and reconcile snapshots before resuming. The helper renews
at 24 hours or less and refuses renewal while this client's stream or pending
commands remain active. It is called explicitly; there is no daemon scheduler.
Do not assume old/new token overlap. A new keypair must be coordinated across
every process using that account. Keep website `bond` sessions separate.

Sockets independently expire after **24 hours** even when a session has weeks
remaining. Reconnect/resubscribe before that deadline, and reconcile REST state.
The bounded stream performs at most three reconnects and does not renew SIWE
silently. A real OEMS still needs durable command reconciliation and a scheduler.

## Verification

The [Galileo verification summary](galileo-live-verification.md) records the
checked flows and remaining limits as of 16 October 2026. It covers session
reuse/renewal, private subscriptions, credential compatibility, named-account
deposit credit and bounded order placement/cancellation. Fills, fee/rebate
behavior, nonempty cancel-all and sustained throughput remain untested.
