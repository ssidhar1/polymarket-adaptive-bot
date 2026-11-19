# Live lifecycle readiness (dormant; paper-only runtime)

This document describes the isolated lifecycle implementation. It is **not**
authorization to route orders and does not make live execution production-ready.

## Runtime invariant

- Docker still launches `python -m src.main --mode paper`.
- `src.main.run_live()` deliberately exits with code 2. It does not create an
  authenticated client or submit orders.
- The lifecycle module is not imported by the paper strategy loop.
- All lifecycle tests use mocked SDK clients. CI does not need a wallet, API
  credentials, funds, or a live Polymarket endpoint.

## Implemented isolated lifecycle

- Durable SQLite intent journal and schema migration for GTD expiry.
- Account-trade pagination and immutable trade-ID deduplication.
- Taker- and maker-side order-fill association.
- Partial-fill quantity tracking; exchange wallet positions remain the
  authoritative source for current shares, cost basis and marked P&L.
- Restart reconciliation of open orders, account trades and wallet positions.
- Recovery of lost submit acknowledgements only when an exact order fingerprint
  identifies one unique exchange order or account fill. Ambiguous matches are
  quarantined for manual reconciliation.
- GTD expiry cancellation; cancellation acknowledgements are not treated as
  final until the order disappears from a subsequent complete open-order read.
- Passive SELL limit exits against a freshly reconciled position, with token
  balance/allowance, book freshness, spread, quantity, notional and open-order
  caps. These are not guaranteed emergency exits and never cross the spread.
- Bounded retries for read-only calls, including restarting paginated reads from
  page one after transient failures.
- No automatic retry for order submission or cancellation after an ambiguous
  network outcome.

## Important accounting limitation

The journal stores exchange-reported fee-rate basis points for each fill. It
does not invent a fee formula or report a fabricated net realized P&L. Fees,
settlement status and any adjustments must be reconciled against authoritative
exchange records before a net-performance report is trusted.

## Before enabling any strategy-driven live routing

A separate release review is still required for:
1. End-to-end verification against the exact wallet type and authentication
   model intended for deployment.
2. Account-level fee/settlement reconciliation and fee calculation tests.
3. Exit policy thresholds, order replacement/chasing and explicit handling of
   passive exits that never fill.
4. Wallet-wide order visibility if deposit wallets or session keys are used.
5. Operational alerting, kill switch, API credentials rotation and manual
   recovery runbook.
6. Controlled canary with minimal funds only after independent review.

Never set live activation variables on Northflank as part of the current
paper-testing deployment.
