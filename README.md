# BondPerpDEX market-maker examples

A small Python package with a reusable `BondPerpDexClient` and testnet market-making examples.

[Quick start](#quick-start) · [Examples](#examples) · [Safety](#safety) · [Checks](#checks) · [Protocol reference](docs/protocol.md)

## Quick start

Requires **Python 3.12+** and a POSIX shell. From this directory:

```sh
sh install.sh
.venv/bin/python -m bond_perpdex
```

The installer creates `.venv`. The default demo signs two BTC quotes, cancels them,
and confirms no open orders—**no credentials, network requests, or transactions**.
To customize it, edit `config.example.json` and add `--config config.example.json`
to the run command.

## Examples

These are the existing CLI and client entry points, not separate numbered scripts.

| # | Entry point | What it does |
|---|---|---|
| 01 | `.venv/bin/python -m bond_perpdex` | Runs one offline dry-run maker cycle: quote, sign, cancel, verify cleanup. |
| 02 | `.venv/bin/python -m bond_perpdex --testnet-read-only` | Reads public testnet depth over REST and WebSocket; no auth or orders. |
| 03 | [`authenticate()`, `submit_order()`, `cancel_order()`, `cancel_all()`](docs/protocol.md#explicitly-enable-the-real-testnet-sdk) | Opt-in live testnet SIWE auth and signed REST orders/cancellations. |
| 04 | [`user_events()`](docs/protocol.md#private-websocket-behavior-and-source-caveats) | Opens the private user stream with bounded reconnect, re-logon, and resubscription. |

The client is reusable independently of the demo strategy. See the
[integration examples and protocol details](docs/protocol.md) for real testnet usage.

## Safety

- **Testnet only:** chain **16602**, pinned endpoints and runtime-registry VirtualBooks.
  This is an example project, **not a published or official SDK**.
- **Live access is opt-in:** private calls require `allow_live_private=True`;
  order placement and cancellation also require `allow_live_orders=True`.
- **Keep credentials out of files and logs.** Supply `BOND_TESTNET_WALLET_KEY` through
  a secret manager into the process environment. Use a dedicated testnet wallet;
  never fund the public demo identities.
- **Supervised use only:** unknown command outcomes halt new orders. Confirm terminal
  cancellation before replacing quotes. Reconnect does not replay orders or recover
  missed events. Live deployment behavior has not been verified.
- **Partial Binance API parity only:** familiar routes do not imply full compatibility.
  The source's private WS logon signature does not bind the timestamp or API key;
  read the [protocol caveats](docs/protocol.md#private-websocket-behavior-and-source-caveats)
  before enabling private access.

## Checks

```sh
.venv/bin/ruff format --check src tests
.venv/bin/ruff check src tests
.venv/bin/pytest -q
.venv/bin/pytest -q tests/test_smoke.py
```

Tests mock HTTP/WebSocket transports and block sockets/DNS. The smoke runs the CLI offline.
The sole protocol authority is `bond-perpdex` at
`34869838ed5588f6a4686f6663e824212287ddb3`; exact source references and signing details
are in [docs/protocol.md](docs/protocol.md#wire-compatibility).
