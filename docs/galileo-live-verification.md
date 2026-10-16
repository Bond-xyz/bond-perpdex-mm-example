# Galileo integration verification

These bounded integration checks completed on **16 October 2026** against
`https://perpdex-testnet.bond.xyz`, Galileo chain **16602**. They cover the
standalone Python examples and quick-start profiles, together with venue
WebSocket compatibility checks.

| Area | Observed result |
| --- | --- |
| Setup and configuration | Make entrypoints, environment/key setup, editable offline defaults, argument forwarding and redacted configuration display passed. |
| Public data | REST health, market metadata and depth reads passed. The latest public depth-stream check timed out at the five-second receive deadline; public streaming did not pass that check. |
| Account and session | Explicit named-account sign-in, identity lookup, private reads, persistent session reuse and renewal passed; renewal invalidated the old token. |
| Private events | Signed subscriptions, cancellation/position notifications, REST reconciliation and bounded quiet-stream completion passed. |
| Named-account funding | A deposit to the selected name was credited to its perpetual collateral balance. |
| Funded orders | Bounded REST and WebSocket placement/cancellation checks passed. Accepted test orders ended canceled, with zero fills, positions or remaining open orders. |
| Cancellation compatibility | Signed REST cancel-one and empty-account WebSocket cancel-all passed. Public REST cancel-all returned 405. |
| Credential compatibility | Provisioned keys were rejected for private WebSocket authentication and public account queries; SIWE sessions supported the complete flow. |

The signing checks included verified chain/contract context and live market
filters. Order expiry accounts for both Endpoint and client wall time. The
sample's cancellation flow checks terminal order state after a cancellation
receipt, and its bounded stream reconciles REST before completing.

## Scope and limits

These results establish bounded testnet integration behavior. They do not verify
fills, fee/rebate economics, nonempty WebSocket cancel-all, sustained throughput,
production behavior or an unattended OEMS renewal scheduler. No automatic retry
or replay of an uncertain mutation is supported.

Local tests block sockets/DNS and use mocked transports. They cover signing,
credential/cache validation, renewal boundaries, risk limits, stream completion
and uncertain outcomes; they do not substitute for funded live checks.

See the [OEMS integration guide](oems-integration.md) for the six integration
answers and the [configuration guide](configuration.md) for variables, defaults
and consuming modules.
