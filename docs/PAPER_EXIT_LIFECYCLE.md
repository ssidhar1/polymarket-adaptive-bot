# Paper-only automatic exit lifecycle

## Runtime path
When `exit_policy.enabled` is true, the paper loop evaluates each open position after checking authoritative market resolution. The evaluator receives the average entry price, remaining shares, opening timestamp, and a fresh executable best bid. Midpoint marks and missing bid depth are not accepted.

Supported triggers:
- take-profit threshold
- stop-loss threshold (highest priority)
- maximum holding time

A triggered exit is simulated as a partial taker sell. Quantity is capped by both the configured fill fraction and displayed best-bid size. The fill price is reduced by configured paper slippage. The configured Polymarket-style fee approximation is applied to the simulated exit notional. This is a research model, not exchange-reported fee data or a promise of execution.

## Durable replay
`paper_lifecycle.db` stores entry fills, partial sell fills, and authoritative market settlements in SQLite with unique event IDs. The ledger reconstructs remaining shares, weighted entry price, allocated entry fees, realized gross P&L, modeled exit fees, and realized net paper P&L after restart.

The journal is written before in-memory position mutation. On startup, the position tracker is reconciled from the journal. A bankroll reset is refused while journaled positions remain open so it cannot orphan exposure.

## Accounting
For a partial sell:
`gross = (sell_price - weighted_entry_price) * shares_sold`

`net = gross - allocated_entry_fees - modeled_exit_fee`

For a market resolution, the remaining quantity is settled at the authoritative 0/1 outcome and remaining entry fees are allocated proportionally. All P&L is paper P&L under the configured fee/slippage assumptions. It is not actual trading profit, not a fee-certified exchange statement, and not evidence of strategy profitability.

## Safety boundary
- This lifecycle is only called from `run_paper`.
- The dormant live adapter and `run_live()` remain disabled.
- No API order, wallet transaction, or real sell is submitted.
- Fresh executable bid timestamp and displayed bid depth are required.
- Exit fill events are idempotent; oversells are rejected.
- Northflank deployment and runtime behavior must be verified separately after redeploying `main`.
