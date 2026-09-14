# V3 Transaction Ledger

## Status

This is the first isolated V3 foundation. It does **not** replace the current
`dashboard/data/portfolio.json` write path yet and does not migrate any existing
portfolio/operation data.

## Source-of-truth direction

V3 will treat immutable economic events as the source of truth. Current and
historical holdings will later be derived by replaying those events.

The critical time fields are deliberately separate:

- `effective_at`: when the economic event actually happened.
- `entered_at`: when the event was recorded in this system.

This allows a transaction entered in 2026 to truthfully represent a trade that
occurred in 2024 or 2025.

## Storage

The first ledger repository uses SQLite and append-only inserts.

Monetary/quantity fields are represented as `Decimal` in Python and stored as
text in SQLite. This avoids binary floating-point drift in a long-lived ledger.

A broker-provided `external_trade_id` is unique within one account so repeated
imports can be rejected safely. Separate accounts may reuse the same external
identifier.

## Initial event vocabulary

- `BUY`
- `SELL`
- `OPENING_POSITION`
- `DEPOSIT`
- `WITHDRAW`
- `FX`
- `DIVIDEND`
- `TRANSFER_IN`
- `TRANSFER_OUT`
- `SPLIT` (reserved for the corporate-action stage)
- `REVERSAL`

`SPLIT`, reversal semantics, transfer cost-basis behavior, and dividend tax
handling are not wired into portfolio replay yet.

## Safety boundary for this checkpoint

At this checkpoint:

1. No existing API writes to the V3 ledger.
2. No V3 code writes to `portfolio.json`.
3. No existing operation/backups are imported or deleted.
4. No dashboard/front-end behavior changes.
5. The next checkpoint is the replay engine, tested entirely against temporary
   ledgers before any compatibility bridge is added.

## Deferred time-zone rule

Explicit offsets are preserved in `effective_at`. Natural-language date/time
resolution (for example `昨天`, `去年3月12号`, or broker-local timestamps) is a
separate V3 date-resolver step and must be decided before voice/history input is
connected to the ledger.

## Replay checkpoint

The replay engine is now implemented in isolation from the production
portfolio write path.

Supported replay semantics:

- `BUY`: moving-average cost basis; buy fees/taxes are capitalized.
- `OPENING_POSITION`: establishes a historical baseline and fails if earlier
  position history already exists for the same account/code/currency.
- `SELL`: reduces quantity/cost at moving-average cost and records realized P&L;
  sell fees/taxes reduce realized P&L.
- `DEPOSIT` / `WITHDRAW`: replay explicit cash events; withdrawals fail closed
  if they would make cash negative.
- `FX`: atomically moves explicit source cash to counter-currency cash.
- `DIVIDEND`: adds net dividend cash after fee/tax and records instrument income
  when a code is present.
- `as_of=YYYY-MM-DD`: returns state through the end of the reported/local
  calendar date, not the UTC-converted date.

Deliberately unsupported in replay at this checkpoint:

- `SPLIT`
- `TRANSFER_IN` / `TRANSFER_OUT`
- `REVERSAL`
- margin/negative-cash settlement
- automatic BUY/SELL cash settlement

Unsupported event types raise `ReplayError` instead of being silently ignored.
