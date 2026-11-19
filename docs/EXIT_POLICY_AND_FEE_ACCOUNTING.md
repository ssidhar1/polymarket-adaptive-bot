# Exit policy and fee accounting (paper validation only)

## Exit policy
`src.execution.exit_policy.evaluate_exit` is a deterministic, side-effect-free
decision function. It supports take-profit, stop-loss, and maximum-holding-time
triggers. It requires an executable best bid and a fresh timezone-aware book
timestamp. A midpoint, stale quote, future opening time, or invalid price fails
closed. Stop-loss takes precedence over time stop, which takes precedence over
take-profit.

The function does not place orders, simulate fills, cancel orders, or promise
that a passive exit will execute. Its gross P&L output excludes fees. Thresholds
are disabled by default and are not connected to the paper strategy or dormant
live adapter. Use replay tests before wiring any policy into a trading loop.

## Fee accounting
The exchange's `fee_rate_bps` is retained as a rate field only. It is not a
USDC fee amount. `explicit_fee_usdc` accepts only explicit `fee_usdc` or
`fee_amount_usdc` fields. Missing fees remain missing; no assumed fee formula
is applied. Fee coverage does not establish realized net P&L: complete fills,
settlement proceeds, adjustments, and complete explicit fees are still needed.

## Runtime safety
- Docker continues to launch paper mode.
- `run_live()` remains hard-disabled.
- Exit-policy functions are not wired to order submission.
- No live orders, wallet credentials, or Northflank environment changes are made.
