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

## Legacy dashboard compatibility checkpoint

`replay_state_to_portfolio()` provides a read-only boundary from exact V3
`Decimal` replay state into the float-based raw portfolio shape already consumed
by the existing valuation/dashboard layer.

Important constraints:

- Decimal-to-float conversion happens only at this compatibility boundary.
- No legacy `portfolio.json` file is written by the adapter.
- Current prices are supplied explicitly; when absent, average cost is used as a
  neutral compatibility fallback rather than fetching or inventing a quote.
- V3 realized P&L and dividend income are preserved as extension metadata even
  though the current legacy valuation code does not aggregate them yet.
- `backend.services` now lazily imports `AgentService`, allowing pure valuation
  and ledger modules to be imported/tested without booting the optional LLM
  dependency stack.

## Historical input normalization checkpoint

The V3 input layer now resolves common explicit historical time expressions
without asking an LLM to invent dates. Supported deterministic forms include:

- ISO dates such as `2025-03-12`
- Chinese dates such as `2025年3月12日`
- `今天` / `昨天` / `前天`
- `去年3月12日` / `今年...` / `前年...`
- `15:30` and common Chinese clock forms such as `下午3点30分`

No date means "now". Date-only input is retained as date precision in event
metadata so confirmation UI can make the missing clock time visible.

Existing bookkeeper change semantics map to V3 events as follows:

- incremental buy/add -> `BUY`
- sell/reduce -> `SELL`
- absolute position baseline -> `OPENING_POSITION`
- deposit/withdraw -> corresponding cash event
- absolute cash baseline -> `OPENING_CASH`
- legacy withdraw+deposit FX pair -> one atomic `FX` event

`OPENING_CASH` was added because a known historical cash balance is a baseline,
not a fake deposit.

## Read-only HTTP API checkpoint

The main FastAPI app now exposes a read-only V3 surface under /api/ledger-v3:

- GET /transactions with optional account/code/date-range filters.
- GET /state?as_of=... for current or historical replay state.
- GET /portfolio-compat?as_of=... for the legacy dashboard-compatible raw portfolio shape.

Exact ledger decimals are returned as strings. Date-only transaction filters use
the event's reported/local calendar date, while datetime ordering uses absolute
instants across timezone offsets. Invalid historical as_of values fail with
HTTP 400 instead of producing server errors.

No write endpoint is exposed in this checkpoint.

## Server-side pending confirmation checkpoint

Structured V3 writes now follow a two-step confirmation flow:

1. POST /api/ledger-v3/preview validates the complete projected history and creates a server-side pending batch.
2. POST /api/ledger-v3/confirm/{pending_id} acquires a SQLite write lock, reloads the latest history, replays it with the pending events, and only then atomically inserts the whole batch and marks the pending item confirmed.

Pending batches expire, cannot be confirmed twice, and are not transactions themselves.
If history changes after preview in a way that makes the pending batch invalid, confirm fails and no proposed event is written.
GET /api/ledger-v3/pending/{pending_id} exposes confirmation status.
