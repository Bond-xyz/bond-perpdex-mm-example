# BondPerpDEX OEMS integration examples

Small Python examples for **Galileo testnet (chain 16602)**, aligned with
[super-app #897](https://github.com/Bond-xyz/bond-super-app/pull/897) and
[#898](https://github.com/Bond-xyz/bond-super-app/pull/898). Start offline.
Private examples make real requests; order writes require explicit permission.
This is reference source, not a published SDK or an unattended market maker.

## Start offline

Python 3.12+ and a POSIX shell are required.

```sh
sh install.sh
.venv/bin/python examples/01_signing_offline.py
.venv/bin/python -m bond_perpdex_client
.venv/bin/python examples/04_provisioned_key_headers.py
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
| `04_provisioned_key_headers.py` | Synthetic managed-key writer REST headers; no credentials read and no network. |
| `python -m bond_perpdex_client --testnet-read-only` | Public exchangeInfo, depth and bounded market-stream reads; no credentials or orders. |

## Select your account and retain its credentials

The website enables trading and deposits into each wallet's **`bond`** subaccount.
A maker can use **`tradegods-z`** instead. Names are scoped to the owner wallet,
not globally reserved; a new name has separate balances. Live sign-in requires
an explicit name and persistent Ed25519 key. It never silently selects `bond`.

Provision the owner wallet through `BOND_TESTNET_WALLET_KEY` in your secret manager.
Create an Ed25519 PKCS#8 PEM through your key manager, retain it outside this repo,
and restrict its permissions to `0600`. Then run:

```sh
.venv/bin/python examples/02_private_rest.py \
  --subaccount tradegods-z --ed25519-key /secure/galileo-ed25519.pem \
  --session-file /secure/galileo.session.json

.venv/bin/python examples/03_private_stream.py \
  --subaccount tradegods-z --ed25519-key /secure/galileo-ed25519.pem \
  --session-file /secure/galileo.session.json --max-events 5 --idle-timeout 15
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

There are **no hardcoded live VirtualBooks**. Obtain the operator-verified
[canonical Galileo manifest](https://github.com/Bond-xyz/bond-shared-registry/blob/main/envs/og-testnet-staging/perpdex.json)
from Bond, confirm it matches the deployed contract set, and save a local copy.
Repository access may be required. `exchangeInfo` supplies filters, not the numeric
product-ID/VirtualBook mapping. A registry file is explicit input, not proof of
its own deployment validity. Refresh it after rotations.

Only after funding the chosen subaccount and choosing price/size, append these
arguments to example 02 (the environment values must be operator supplied):

```sh
--place --side BUY --price "$BOND_LIMIT_PRICE" --quantity "$BOND_ORDER_QUANTITY" \
--registry /secure/perpdex.local.json --rpc-url "$BOND_GALILEO_RPC_URL"
```

Live order preparation checks Galileo RPC chain identity and reads the verified
Endpoint's `getTime()` for signed expiry. Expiry is 60 seconds after the later
of Endpoint time and the client clock: ingress also checks wall time, while
Endpoint time can lag between settlement batches. The client validates live precision,
tick/step/notional filters and its configured risk bounds. Orders can execute.
No deposits are implemented; the website cannot select a named account to fund.
See [funding and all six integration answers](docs/oems-integration.md).

## Deployment limits

- Use SIWE sessions for private events and the full OEMS read/trade flow. Managed
  `x-bond-api-key` credentials remain REST-only; current public account-reader
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
.venv/bin/ruff format --check src tests examples
.venv/bin/ruff check src tests examples
.venv/bin/pytest -q
.venv/bin/python examples/01_signing_offline.py
.venv/bin/python -m pip check
```

Tests block sockets/DNS and use mock transports. Source/runtime checks on
16 October 2026 bind the guidance to backend `df07bef07f7e6098861fae2a8435132aadff11a4`;
see [verification](docs/oems-integration.md#verification) and the
[funded TradeGods run](docs/galileo-live-verification.md). Offline tests do not
establish funded behavior; the live run verifies deposit credit and place/cancel,
with zero fills. Fill/fee behavior and sustained throughput remain untested.
