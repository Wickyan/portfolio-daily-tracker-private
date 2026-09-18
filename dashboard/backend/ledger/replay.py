"""Deterministic replay of immutable ledger events into portfolio state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Tuple

from .models import Transaction, TransactionType, normalize_event_time


PositionKey = Tuple[str, str, str]
CashKey = Tuple[str, str]
ZERO = Decimal("0")


@dataclass
class PositionState:
    account: str
    code: str
    currency: str
    quantity: Decimal
    total_cost: Decimal
    name: str = ""
    instrument_id: Optional[str] = None

    @property
    def average_cost(self) -> Decimal:
        return self.total_cost / self.quantity if self.quantity else ZERO


@dataclass
class ReplayState:
    positions: Dict[PositionKey, PositionState] = field(default_factory=dict)
    cash: Dict[CashKey, Decimal] = field(default_factory=dict)
    realized_pnl: Dict[PositionKey, Decimal] = field(default_factory=dict)
    dividend_income: Dict[PositionKey, Decimal] = field(default_factory=dict)
    applied_transaction_ids: List[str] = field(default_factory=list)

    def get_position(self, account: str, code: str, currency: str) -> Optional[PositionState]:
        return self.positions.get((account, code.upper(), currency.upper()))

    def get_cash(self, account: str, currency: str) -> Decimal:
        return self.cash.get((account, currency.upper()), ZERO)

    def get_realized_pnl(self, account: str, code: str, currency: str) -> Decimal:
        return self.realized_pnl.get((account, code.upper(), currency.upper()), ZERO)


class ReplayError(ValueError):
    pass


def _position_key(tx: Transaction) -> PositionKey:
    return (tx.account, str(tx.code or "").upper(), str(tx.currency or "").upper())


def _sort_timestamp(value: str) -> float:
    """Return a deterministic absolute ordering key for normalized ISO time.

    Offset-aware timestamps are converted to UTC. Naive timestamps are treated
    as UTC *for ordering only*. Natural-language/broker timezone resolution is
    intentionally a later input-layer responsibility.
    """
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).timestamp()


def _as_of_timestamp(value: str) -> float:
    raw = str(value or "").strip()
    if len(raw) == 10:
        day = datetime.fromisoformat(raw).date()
        return datetime.combine(day, time.max, tzinfo=timezone.utc).timestamp()
    return _sort_timestamp(normalize_event_time(raw))


def replay_transactions(
    transactions: Iterable[Transaction],
    *,
    as_of: Optional[str] = None,
) -> ReplayState:
    """Replay transactions using moving-average cost basis.

    BUY fees/taxes are capitalized into cost basis.
    SELL fees/taxes reduce realized P&L.
    BUY/SELL currently do not infer cash movements; explicit cash events remain
    the cash source of truth until a settlement policy is introduced.
    """
    validated = [tx.validated() for tx in transactions]
    validated.sort(
        key=lambda tx: (
            _sort_timestamp(str(tx.effective_at)),
            _sort_timestamp(str(tx.entered_at)),
            tx.transaction_id,
        )
    )
    as_of_raw = str(as_of or "").strip()
    date_only_cutoff = None
    if as_of_raw and len(as_of_raw) == 10:
        try:
            date.fromisoformat(as_of_raw)
        except ValueError as exc:
            raise ValueError("as_of must be ISO date/datetime") from exc
        date_only_cutoff = as_of_raw
    instant_cutoff = _as_of_timestamp(as_of_raw) if as_of_raw and not date_only_cutoff else None
    state = ReplayState()

    for tx in validated:
        if date_only_cutoff is not None:
            # A date query means the reported/local calendar date, not the UTC
            # date after offset conversion. This prevents e.g. 00:30 +08:00 on
            # Mar 13 from leaking into a Mar 12 historical snapshot.
            if str(tx.effective_at)[:10] > date_only_cutoff:
                continue
        elif instant_cutoff is not None and _sort_timestamp(str(tx.effective_at)) > instant_cutoff:
            continue
        _apply_transaction(state, tx)
        state.applied_transaction_ids.append(tx.transaction_id)
    return state


def _apply_transaction(state: ReplayState, tx: Transaction) -> None:
    event_type = tx.event_type

    if event_type in {TransactionType.BUY, TransactionType.OPENING_POSITION}:
        _apply_buy_or_opening(state, tx)
        return
    if event_type == TransactionType.SELL:
        _apply_sell(state, tx)
        return
    if event_type in {TransactionType.DEPOSIT, TransactionType.OPENING_CASH, TransactionType.WITHDRAW}:
        _apply_cash(state, tx)
        return
    if event_type == TransactionType.FX:
        _apply_fx(state, tx)
        return
    if event_type == TransactionType.DIVIDEND:
        _apply_dividend(state, tx)
        return

    raise ReplayError(f"event type not replayable yet: {event_type.value}")


def _apply_buy_or_opening(state: ReplayState, tx: Transaction) -> None:
    key = _position_key(tx)
    quantity = tx.quantity or ZERO
    price = tx.price or ZERO
    event_cost = quantity * price + tx.fee + tx.tax
    existing = state.positions.get(key)

    if tx.event_type == TransactionType.OPENING_POSITION and existing is not None:
        raise ReplayError(f"opening position conflicts with existing history: {key}")

    if existing is None:
        state.positions[key] = PositionState(
            account=tx.account,
            code=str(tx.code or "").upper(),
            currency=str(tx.currency or "").upper(),
            quantity=quantity,
            total_cost=event_cost,
            name=str(tx.name or ""),
            instrument_id=tx.instrument_id,
        )
        return

    existing.quantity += quantity
    existing.total_cost += event_cost
    if tx.name:
        existing.name = tx.name
    if tx.instrument_id:
        existing.instrument_id = tx.instrument_id


def _apply_sell(state: ReplayState, tx: Transaction) -> None:
    key = _position_key(tx)
    existing = state.positions.get(key)
    if existing is None:
        raise ReplayError(f"cannot sell missing position: {key}")

    quantity = tx.quantity or ZERO
    if quantity > existing.quantity:
        raise ReplayError(
            f"sell quantity {quantity} exceeds holding {existing.quantity}: {key}"
        )

    allocated_cost = existing.average_cost * quantity
    proceeds = quantity * (tx.price or ZERO)
    realized = proceeds - allocated_cost - tx.fee - tx.tax
    state.realized_pnl[key] = state.realized_pnl.get(key, ZERO) + realized

    existing.quantity -= quantity
    existing.total_cost -= allocated_cost
    if existing.quantity == ZERO:
        del state.positions[key]


def _apply_cash(state: ReplayState, tx: Transaction) -> None:
    key = (tx.account, str(tx.currency or "").upper())
    current = state.cash.get(key, ZERO)
    amount = tx.amount or ZERO
    if tx.event_type == TransactionType.OPENING_CASH:
        if key in state.cash:
            raise ReplayError(f"opening cash conflicts with existing history: {key}")
        target = amount
    else:
        target = current + amount if tx.event_type == TransactionType.DEPOSIT else current - amount
    if target < ZERO:
        raise ReplayError(f"cash would become negative for {key}: {target}")
    state.cash[key] = target


def _apply_fx(state: ReplayState, tx: Transaction) -> None:
    source_key = (tx.account, str(tx.currency or "").upper())
    target_key = (tx.account, str(tx.counter_currency or "").upper())
    source_after = state.cash.get(source_key, ZERO) - (tx.amount or ZERO)
    if source_after < ZERO:
        raise ReplayError(f"FX source cash insufficient for {source_key}: {source_after}")
    state.cash[source_key] = source_after
    state.cash[target_key] = state.cash.get(target_key, ZERO) + (tx.counter_amount or ZERO)


def _apply_dividend(state: ReplayState, tx: Transaction) -> None:
    currency = str(tx.currency or "").upper()
    cash_key = (tx.account, currency)
    net_amount = (tx.amount or ZERO) - tx.fee - tx.tax
    if net_amount < ZERO:
        raise ReplayError("dividend net amount cannot be negative")
    state.cash[cash_key] = state.cash.get(cash_key, ZERO) + net_amount

    if tx.code:
        key = _position_key(tx)
        state.dividend_income[key] = state.dividend_income.get(key, ZERO) + net_amount
