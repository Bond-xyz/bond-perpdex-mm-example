# Galileo OEMS integration guide

These are the same six integration answers published in super-app
[#897](https://github.com/Bond-xyz/bond-super-app/pull/897) and
[#898](https://github.com/Bond-xyz/bond-super-app/pull/898). Deployment statements
below were checked on **16 October 2026** against serving backend
[`df07bef07f7e6098861fae2a8435132aadff11a4`](https://github.com/Bond-xyz/bond-perpdex/tree/df07bef07f7e6098861fae2a8435132aadff11a4).
See the [protocol reference](protocol.md) for signing vectors and client boundaries.

## 1. Which account does the website fund, and how do makers create their own?

`https://testnet.bond.xyz/trade` enables trading and deposits into **`bond`**.
This is a configured subaccount name, not a shared custodial wallet: the identity
is **owner wallet + name**. Frontend source's `default` fallback is not the
website's selected name. Connecting a wallet alone does not create a session;
use Enable trading. There is no global username reservation: the database enforces
uniqueness of the wallet/name pair. Another wallet can use the same name.

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

No supported public REST discovery endpoint returns the signing mapping.
Live `GET /fapi/v1/exchangeInfo` contains symbols, precision and market filters,
not numeric product IDs or signing addresses. Obtain the active `markets` and
`contracts` from Bond's
[canonical Galileo registry](https://github.com/Bond-xyz/bond-shared-registry/blob/main/envs/og-testnet-staging/perpdex.json).
Repository access may be needed; otherwise obtain the verified manifest from Bond.
No anonymous manifest API is promised.

Use `SigningRegistry.from_file(path)` and supply it as
`TestnetConfig(signing_registry=registry)`. It validates the Galileo chain,
nonzero address shape, unique product IDs/VirtualBooks and market presence, and
retains the file's SHA-256. This is schema validation, not automatic verification
that the file matches the deployed release. The operator must confirm provenance
and contract-set identity. Live trading has no fallback to offline fixtures or
historical deployment addresses.

At startup refresh `exchangeInfo`; stop for missing markets, signing context or
filters. Refresh the registry after rotations. Read the verified Endpoint's
`getTime()` on chain 16602 for signed expiry (`read_endpoint_time`); do not derive
it from a workstation clock. A VirtualBook can be an EIP-712 domain address
without runtime code, as described by the active manifest's `bookDomain`; do not
substitute a contract just because it has bytecode.

## 4. Is there a REST cancel-all endpoint?

The backend implements **`DELETE /fapi/v1/openOrders`**, form-encoded with
`symbol`, `newClientOrderId`, `timestamp`, optional `recvWindow`, and Ed25519
`signature`. It affects all orders for **one symbol in one authenticated
subaccount**, not every symbol/account. Managed keys additionally require the
appropriate `trade:write` scope and signed `x-bond-*` headers.

**Current deployment caveat:** Galileo's gateway routes this exact path to a
GET-only account reader even for DELETE, so the public REST request returns
**405**. `cancel_all()` therefore uses signed **`openOrders.cancelAll`** on the
trading WebSocket, with the same signed fields plus SIWE `apiKey`. It never falls
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
| SIWE `x-api-key` REST | The managed-key 120-request counter does not apply. Writer HTTP has per-IP token-bucket protection: one token/ms with a 100,000-token burst in the inspected config. Account reads are served separately. These settings are not a partner throughput allocation. |
| Managed `x-bond-api-key` REST | 120 requests per rolling 60 seconds per key on writer routes installing managed-key authentication. A 429 includes `Retry-After`. Current public account-reader routes do not install that middleware and require SIWE. |
| Trading WebSocket commands | No fixed per-second/per-minute counter is implemented for `order.place`, `order.cancel` or `openOrders.cancelAll`. Processing is sequential per connection with a 10-second handling deadline. Capacity, admission and backpressure remain binding. |
| Shared REST/WS counter | None for trading commands. HTTP admission and upgrade guards are separate; frames after upgrade do not traverse the HTTP limiter. |
| WebSocket upgrades | 300 new connections per rolling 5 minutes per IP per service process. The trading paths share their service limiter; public market-data upgrades have a separate one. |
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
WebSocket authentication shape. Public account-reader routes also require SIWE.
Example 04 is consequently an **offline header-shape example for a writer REST
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

On 16 October 2026, the active release binding still identified backend
`df07bef07f7e6098861fae2a8435132aadff11a4` and registry digest
`cf93d95908d454404879fa969dd8b1857f41b389f9b6a26def96eda9689ecdfc`.
Public health/ready, exchangeInfo and venue-status reads returned 200.
The gateway/runtime inspection and approved disposable unfunded probe for
super-app #897/#898 verified SIWE reads, signed private subscription, empty
WebSocket cancel-all, REST cancel-all returning 405, managed-key creation/revocation,
managed-key rejection by private WS/account-reader, and old-token invalidation
on renewal. Those checks made no deposits or order placements.

The updated Python client was also exercised with a fresh unfunded disposable
wallet: SIWE sign-in, owner-only cache creation and reuse via `/auth/me`, empty
REST openOrders, timestamp-bound private subscription, empty WebSocket cancel-all,
and old-token rejection after renewal all passed. Temporary session files were
removed. WebSockets use the same verified CA trust roots as HTTP; no certificate
or hostname checks were disabled. No deposits or order placements were made.

Local tests use blocked sockets/DNS and mocked HTTP/WebSocket transports.
They verify cryptographic vectors, named accounts, registry loading, credential
reuse/renewal and restricted files, cancellation receipts and unknown outcomes.
They do not establish funded admission, fills, fees or sustained throughput.
