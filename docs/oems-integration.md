# OEMS integration guide

This page answers the four partner questions directly. The Python examples and
the bot-key, fee, and rate-limit explanations use the pinned `bond-perpdex`
source revision `34869838ed5588f6a4686f6663e824212287ddb3` (2026-09-23).
The relevant source files were unchanged on `main` at `09834cbf` (2026-09-24).
**Source review is not live Galileo testnet verification.** Recheck deployment
identity and responses before relying on this in production software.

## 1. Order signing

For the documented testnet perpetuals path, **yes, `signedOrder` is required**.
The Ed25519 request signature authenticates the account request; it does not
replace the wallet's Vertex EIP-712 authorization. The server's unsigned
legacy code path does not establish permission to send unsigned orders in a
canonical release environment. REST `POST /fapi/v1/order` and WebSocket
`order.place` accept the same structured order semantics.
The [source API reference](https://github.com/Bond-xyz/bond-perpdex/blob/34869838ed5588f6a4686f6663e824212287ddb3/docs/public-api/API_REFERENCE.md)
marks the envelope as required on documented testnet perp orders.

The shortest executable reference is
[`examples/01_signing_offline.py`](../examples/01_signing_offline.py). It prints
the synthetic SIWE message and signed order without network access. For a
supervised REST request, follow
[`examples/02_private_rest.py`](../examples/02_private_rest.py). The
`websocket_order_place(intent)` helper converts an already prepared signed
REST order to the WebSocket `{id, method, params}` shape; it does **not** send
the command, so do not submit both representations of one intent. Generate a
fresh request immediately before either submission; the request timestamp is
accepted only within its short `recvWindow`.

The signing sequence is:

1. `GET /auth/nonce`; build the EIP-4361 SIWE message with the Ed25519 public
   key's **raw 32-byte hex** in its statement. Ethereum `personal_sign`
   (EIP-191) signs the whole message. `POST /auth/signin` sends the message,
   wallet signature, `secret_type: "Ed25519"`, and the same public key in
   **SubjectPublicKeyInfo PEM** as `secret_key`. The SIWE nonce is single-use
   for sign-in; it is not an order nonce.
2. Form `sender` from the 20-byte wallet address followed by a 12-byte,
   right-padded subaccount name. This example accepts 1–12 ASCII alphanumeric
   bytes. Sign `Order(bytes32 sender,int128 priceX18,int128 amount,uint64
   expiration,uint64 nonce)` with EIP-712 domain `Vertex`, version `0.0.1`,
   chain `16602`, and the market's pinned VirtualBook. Use a fresh nonzero
   `uint64` order nonce; there is no inferred sequential nonce endpoint.
3. Convert price exactly to X9, then X18; convert base quantity using market
   base precision, then X18. Sell amount is negative. The envelope serializes
   integer fields as decimal strings. `POST_ONLY`/`GTC` uses expiration
   `(3 << 62) | expiry_seconds`. Reject tick/step or precision violations;
   do not round a user's signed intent silently.
4. Put the envelope in compact canonical JSON as `signedOrder`. Sort REST
   request field names, URL-form-encode their exact values, Ed25519-sign those
   bytes, and append base64 `signature`. Use `x-api-key` for the SIWE session.
   For WebSocket `order.place`, send the envelope as an object, include
   `apiKey`, numeric `timestamp`, and the same account signature.

Source details and golden hashes are in [the protocol reference](protocol.md#wire-compatibility).
The available examples cover a POST_ONLY order. Other order types need their
own signed-intent semantics and validation.

## 2. Binance compatibility and fees

**Compatibility is partial.** `/fapi/v1` names and some parameters resemble
Binance USD-M Futures; private authentication, order IDs, cancel responses,
event names and shapes, and some field coverage differ. Treat the following
as **sanitized source-derived examples**, not captured Galileo traffic. The
server can add fields, and the examples omit unrelated optional fields.

Subscription uses `session.logon` followed by `userDataStream.subscribe` with
`params: {}`. A successful subscribe acknowledgement and notification have
different envelopes:

```json
{"id":"request-2","status":200,"result":{"subscriptionId":1}}
{"subscriptionId":1,"event":{"e":"outboundAccountPosition"}}
```

That sparse `outboundAccountPosition` event currently has **no balance array**.
Read the REST account projection after subscribing or reconnecting. Other
account events can look like:

```json
{"subscriptionId":1,"event":{"e":"balanceUpdate","E":1770000000000,"a":"USDC.e","d":"2","T":1770000000000}}
{"subscriptionId":1,"event":{"e":"positionUpdate","E":1770000000000,"I":11,"eventId":"00000000-0000-4000-8000-000000000003","accountNonce":4,"symbol":"BTCUSDCPERP","positionAmt":"0.001","virtualQuoteBalance":"-65","isolatedMargin":"10","reservedMargin":"0","leverage":10,"perpWalletQuoteBalance":"200"}}
```

The `balanceUpdate.d` field is a **balance delta**, not the execution fee.
Order transitions arrive as `executionReport`, with `x` as the transition,
`X` as current status, `i` as order UUID, `n` as the fee, and `N` as fee asset:

```json
{"subscriptionId":1,"event":{"e":"executionReport","E":1770000000000,"I":12,"s":"BTCUSDCPERP","i":"00000000-0000-4000-8000-000000000002","x":"TRADE","X":"PARTIALLY_FILLED","l":"0.001","L":"65000","z":"0.001","n":"0.013","N":"USDC.e","m":true}}
```

That last fee value is **illustrative**, not an observed fee or a claim about
the active fee tier. The matching engine's current fee rates and raw fee
amounts are unsigned/nonnegative and the execution report copies the amount
and `USDC.e` currency. Thus positive `n` is a charge; this implementation does
not demonstrate negative `n` or a maker rebate. Zero is possible. Confirm the
actual fee from a testnet fill and the account's fee-tier endpoint before
reconciling cashflows. Events are best effort; use REST after disconnects or
sequence anomalies. These field and sign claims are grounded in the
[execution-report converter](https://github.com/Bond-xyz/bond-perpdex/blob/34869838ed5588f6a4686f6663e824212287ddb3/services/trading/src/service/execution_report.rs)
and [nonnegative fee types](https://github.com/Bond-xyz/bond-perpdex/blob/34869838ed5588f6a4686f6663e824212287ddb3/core/orderbook-rs/src/balance/fee_rate.rs).

## 3. Authentication and reuse

Two credential families coexist:

| Credential | Used for | Restart and expiry behavior |
| --- | --- | --- |
| SIWE wallet session (`x-api-key` plus Ed25519 account signature) | Examples 02–03, private WebSocket logon, and key management | Server returns `expires_at`; the client keeps token and Ed25519 key only in memory. A new sign-in can replace the old token. The configured legacy lifetime in the inspected source is 30 days; use the response expiry as authority. No automatic renewal is implemented here. |
| Provisioned bot key (`x-bond-api-key` plus Ed25519 `x-bond-signature`) | Scoped private REST bot routes; example 04 | It can be reused after restart while its secret and matching Ed25519 key remain available, its optional expiry has not passed, and it has not been revoked or blocked by its IP/scope policy. It is not accepted for private WebSocket logon in the inspected source. |

[`bot_key.py`](../src/bond_perpdex_client/bot_key.py) shows the provisioned key's
outer signature: `timestamp\nnonce\nMETHOD\npathAndQuery\nsubaccountOrDefault\nsha256hex(rawBody)`.
The nonce is fresh per request and the timestamp window is 60 seconds in the
inspected source. Order placement using this credential still needs the
account form `signature` and wallet `signedOrder`; bot-key headers alone do not
authorize an order. This repository does not implement key creation, rotation,
or a full bot-key order client. It does not persist either credential family.
An application that retains a still-valid SIWE token and its Ed25519 private key
could also resume that session after restart; this example deliberately has no
session import or persistence path. Re-sign-in is explicit and can invalidate
the prior session. A provisioned key is renewed through key management or
rotation, not by repeating `POST /auth/signin`.

## 4. Rate limits

The inspected current source publishes a **120 raw requests / 60 seconds per
provisioned bot key** entry as `RAW_REQUESTS` in `exchangeInfo`. Each covered
private REST request consumes one count, including an order or an account
read. A per-IP governor also applies to HTTP, and private WebSocket upgrades
are limited to **300 per 5 minutes per IP**. These are independent layers, so
`exchangeInfo` alone is not a complete limit description.

`REQUEST_WEIGHT` and `ORDERS` may appear in `exchangeInfo`, but the inspected
source says they are **advertised and not independently metered**. The
per-key counter covers `x-bond-api-key` routes, not the legacy `x-api-key`
session used by examples 02–03. A 429 includes `Retry-After` and code `-1003`;
the inspected source does not expose a Binance-style per-request usage header
for that counter. WebSocket responses have an optional `rate_limits` field,
but do not assume it reports the bot-key REST counter. See the
[source rate-limit reference](https://github.com/Bond-xyz/bond-perpdex/blob/34869838ed5588f6a4686f6663e824212287ddb3/docs/public-api/RATE_LIMITS.md)
for route scope and the current limiter policy.

## Verification boundary

The local tests cover the exact source order hash/form vectors, SIWE public
key encoding, both Ed25519 request schemes, REST request shapes, private
subscription and event parsing, stale data and unknown command handling. They
use mocked HTTP/WebSocket transports and block socket/DNS access. No example
in this repository proves current deployed contract addresses, admission,
order fills, fee tiers, API-key policy, or live rate-limit behavior. Capture
sanitized live responses with a provisioned test account before presenting
them as observed partner examples.
