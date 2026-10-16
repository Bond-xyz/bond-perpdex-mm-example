# Funded TradeGods check — 16 October 2026

The actual Python examples and the matching Node example from super-app
[#897](https://github.com/Bond-xyz/bond-super-app/pull/897)/
[#898](https://github.com/Bond-xyz/bond-super-app/pull/898) ran against
`https://perpdex-testnet.bond.xyz` on Galileo chain **16602**.
This was a bounded integration check, not a continuous market-making run.

## Account and deposit

- Example name: **TradeGods Galileo example**; subaccount: **`tradegods-z`**.
- Dedicated wallet: `0x7B98694Fd4c2b26f8418bb01be64ff4e2B263876`.
- `/auth/me` account UUID: `a2552470-9c31-4cd3-9953-ce715027641c`;
  padded name: `7472616465676f64732d7a00`.
- Approved and deposited **200 mock USDC.e**, product `0`, through the verified
  Endpoint's `depositCollateral(bytes12,uint32,uint128)` with this name.
- Deposit transaction:
  `0xc99ca6d7f872a91f7a0558d73393bc34c344c452b9d94ed5bf529dfc1275b774`,
  block **60424289**. Waited for finalized API credit: perpetual balance **200**,
  pending deposits **0**. No second deposit or separate spot/perp command was sent.

The wallet received 0.02 test 0G for gas. Its keys and session files remain in
owner-only local secret storage so the funded account remains recoverable.
Existing makers and their credentials were not used.

## Results

Both final runs reused the same SIWE session and persistent Ed25519 key.
Each order bought **0.002 BTC**, approximately 1% below the observed best bid,
with notional below **200 USDC**; each was immediately canceled.

| Actual example | Final successful order | Result |
| --- | --- | --- |
| Python `02_private_rest.py --place`, with `03_private_stream.py --idle-timeout 15` | `6d001979-ad53-4b8a-825e-d2edb57a6d3e` at `81304.80` (162.60960 USDC), `POST_ONLY` | REST placement/cancel passed; private cancellation and position events received; stream reconciled REST before exit |
| Node `demo.mjs trade`, identical in #897/#898 | `879efe74-d51b-4d96-a8cb-f525b18a17f7` at `81278.60` (162.55720 USDC), `LIMIT/GTC` | WebSocket place/status/cancel passed; private cancellation event received; signed REST fills lookup returned `[]`; final reconciliation passed |

Final independent order/account reads confirmed **CANCELED**, executed quantity
**0**, **0 open orders**, **0 BTC position**, and **200 USDC.e available**.
The earlier disposable `tradegods-z` run also exercised the actual REST script
twice with cache reuse, private-stream subscription, empty WebSocket cancel-all
and near-expiry credential renewal with old-token rejection.

## Defects found and corrected

The initial Python order was rejected before a write: Endpoint time was
`1792141200`, so Endpoint + 60 produced expiry `1792141260`, already before
normalized ingress time `1792141867`. Preparation now uses the later of client
wall time and Endpoint time plus TTL. The Python TTL is 60 seconds; the Node
GTC example uses four hours. Regression tests cover clock lag, clock lead and
timestamp overflow. The rejected intent was retained and never replayed.

The Python stream's five-second default idle wait ended before the first
successful cancellation. The new `--idle-timeout` accepts 1–60 seconds; 15 seconds
verified cancellation delivery while preserving the five-second RPC deadline.

The first Node run placed and canceled successfully but its final fills lookup
failed signature validation because it sent `order_id`. It now sends the
backend's `orderId`; the corrected whole script exited successfully.

There were four sequential accepted orders during diagnosis and verification,
all canceled with zero fills, plus the initial rejected Python intent. Exact
signed intents were journaled locally before sending; no uncertain command was
retried. [Sanitized receipts and tested-code hashes](live-verification/galileo-2026-10-16.json)
include every accepted order without keys, tokens or signed request bodies.

## Deployment and limits

The active writer and account reader used image
`sha256:73ec2fc9ee7b25e8abf6307ec282b516b9b1b24a210ee5667fe4341146f30df3`.
The release binding identified backend
`df07bef07f7e6098861fae2a8435132aadff11a4` and registry SHA-256
`cf93d95908d454404879fa969dd8b1857f41b389f9b6a26def96eda9689ecdfc`.
Signing context came from that active binding, with chain identity, token
decimals, Endpoint code and live market filters checked before trading.
No application deployment or economic-control change was made.

This verifies named-account funding, bounded order admission/cancellation,
private notifications and reconciliation. It does not verify fills, fee/rebate
economics, nonempty WebSocket cancel-all, sustained throughput or a long-running
OEMS renewal scheduler. Public REST cancel-all still returns 405.

## Quick-start merge and refactor verification

After [PR #3](https://github.com/Bond-xyz/bond-perpdex-mm-example/pull/3) merged
as `847afd9`, the unchanged baseline passed 191 tests. Actual Galileo checks
reproduced one newly added runner defect: a quiet stream reconciled REST and
then raised `Private stream reconnect budget exhausted`. The runner now exits
cleanly after its final reconciled disconnect, while honoring its reconnect
budget and propagating subscription, event-validation and reconciliation errors.

The scripts and quick-start profiles now share account setup and supervised
flows in `workflows.py`. Cache validation/serialization moved to `credentials.py`;
session lifecycle decisions remain in `client_session.py`. The cache schema and
public `Session` imports remain compatible. Cache tests use a named pytest fixture.

Post-refactor verification on the same Galileo binding passed:

- Actual `setup.sh`, offline defaults and spread/quantity overrides, redacted
  config display, public mode, private reads twice, and quiet private streams.
- Actual standalone REST and stream examples. The existing configuration,
  Ed25519 key and cached session remained byte-for-byte unchanged.
- Trade mode without `--allow-live-orders` rejected execution as intended.
- The actual quick-start trade module placed/canceled order
  `95da556c-202f-48f2-952c-cd04fbce3640`: **0.002 BTC POST_ONLY BUY at 81208.60**,
  notional **162.41720 USDC**. The standalone stream received its cancellation
  and position events. Final independent reads showed **CANCELED**, zero fills,
  positions and open orders, and **200 USDC.e** perpetual collateral.
- **207 tests**, Ruff lint/format, shell syntax and dependency checks passed.
  Regression coverage includes quiet-stream completion with zero/two reconnects,
  malformed caches and identity failures, and uncertain submit/cancel outcomes.

An earlier attempt received `ORACLE_ADMISSION_CLOSED` (503). It was not replayed:
its client-ID lookup returned `ORDER_NOT_FOUND`, with zero orders/positions and
unchanged collateral. After reconciliation and operational venue checks, the
successful test used a fresh intent. No new deposits or deployment changes were
made. [Sanitized follow-on evidence and code hashes](live-verification/galileo-quickstart-2026-10-16.json)
record both outcomes. Graphify has not supplied a fresh graph baseline for these
refactors, so no claim is made about its updated coupling/impact scores.
