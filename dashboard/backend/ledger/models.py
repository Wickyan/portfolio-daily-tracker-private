"""Canonical transaction event model for the V3 ledger."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Dict, Optional, Union
from uuid import uuid4


SUPPORTED_CURRENCIES = {"CNY", "USD", "HKD"}


class TransactionType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    OPENING_POSITION = "OPENING_POSITION"
    DEPOSIT = "DEPOSIT"
    OPENING_CASH = "OPENING_CASH"
    WITHDRAW = "WITHDRAW"
    FX = "FX"
    DIVIDEND = "DIVIDEND"
    TRANSFER_IN = "TRANSFER_IN"
    TRANSFER_OUT = "TRANSFER_OUT"
    SPLIT = "SPLIT"
    REVERSAL = "REVERSAL"


def _decimal_number(value: Any, field_name: str) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite decimal number") from exc
    if not number.is_finite():
        raise ValueError(f"{field_name} must be a finite decimal number")
    return number


def normalize_event_time(value: Union[str, date, datetime]) -> str:
    """Normalize a user/event time into an ISO-8601 string.

    A date-only historical record is represented at midnight. We keep any
    supplied timezone offset instead of silently converting it, because the
    ledger must preserve what the user/broker actually reported.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time()).isoformat()

    raw = str(value or "").strip()
    if not raw:
        raise ValueError("effective_at is required")
    if len(raw) == 10:
        try:
            parsed_date = date.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError("effective_at must be ISO date/datetime") from exc
        return datetime.combine(parsed_date, datetime.min.time()).isoformat()

    candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError("effective_at must be ISO date/datetime") from exc
    return parsed.isoformat()


@dataclass(frozen=True)
class Transaction:
    """One immutable economic event.

    ``effective_at`` is when the event really happened.
    ``entered_at`` is when the event was recorded in this system.
    Keeping both is what makes historical backfill safe and auditable.
    """

    event_type: TransactionType
    effective_at: Union[str, date, datetime]
    account: str

    transaction_id: str = field(default_factory=lambda: str(uuid4()))
    entered_at: Union[str, datetime] = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    instrument_id: Optional[str] = None
    code: Optional[str] = None
    name: Optional[str] = None
    currency: Optional[str] = None
    quantity: Optional[Decimal] = None
    price: Optional[Decimal] = None
    fee: Decimal = Decimal("0")
    tax: Decimal = Decimal("0")
    amount: Optional[Decimal] = None

    counter_currency: Optional[str] = None
    counter_amount: Optional[Decimal] = None

    source: str = "manual"
    note: str = ""
    external_trade_id: Optional[str] = None
    reverses_transaction_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def validated(self) -> "Transaction":
        event_type = self.event_type
        if not isinstance(event_type, TransactionType):
            try:
                event_type = TransactionType(str(event_type).upper())
            except ValueError as exc:
                raise ValueError(f"unsupported event_type: {self.event_type}") from exc

        account = str(self.account or "").strip()
        if not account:
            raise ValueError("account is required")

        transaction_id = str(self.transaction_id or "").strip()
        if not transaction_id:
            raise ValueError("transaction_id is required")

        effective_at = normalize_event_time(self.effective_at)
        entered_at = normalize_event_time(self.entered_at)
        currency = str(self.currency or "").upper().strip() or None
        counter_currency = str(self.counter_currency or "").upper().strip() or None

        quantity = _decimal_number(self.quantity, "quantity")
        price = _decimal_number(self.price, "price")
        fee = _decimal_number(self.fee, "fee") or Decimal("0")
        tax = _decimal_number(self.tax, "tax") or Decimal("0")
        amount = _decimal_number(self.amount, "amount")
        counter_amount = _decimal_number(self.counter_amount, "counter_amount")

        if fee < 0 or tax < 0:
            raise ValueError("fee/tax cannot be negative")
        for value, field_name in ((currency, "currency"), (counter_currency, "counter_currency")):
            if value is not None and value not in SUPPORTED_CURRENCIES:
                raise ValueError(f"unsupported {field_name}: {value}")

        position_events = {
            TransactionType.BUY,
            TransactionType.SELL,
            TransactionType.OPENING_POSITION,
            TransactionType.TRANSFER_IN,
            TransactionType.TRANSFER_OUT,
        }
        if event_type in position_events:
            if not str(self.code or "").strip():
                raise ValueError("code is required for position events")
            if currency is None:
                raise ValueError("currency is required for position events")
            if quantity is None or quantity <= 0:
                raise ValueError("quantity must be > 0 for position events")
            if event_type in {TransactionType.BUY, TransactionType.SELL, TransactionType.OPENING_POSITION}:
                if price is None or price < 0:
                    raise ValueError("price must be >= 0 for buy/sell/opening events")

        if event_type in {TransactionType.DEPOSIT, TransactionType.OPENING_CASH, TransactionType.WITHDRAW, TransactionType.DIVIDEND}:
            if currency is None:
                raise ValueError("currency is required for cash events")
            if amount is None or amount <= 0:
                raise ValueError("amount must be > 0 for cash events")

        if event_type == TransactionType.FX:
            if currency is None or counter_currency is None:
                raise ValueError("FX requires source and counter currency")
            if currency == counter_currency:
                raise ValueError("FX currencies must differ")
            if amount is None or amount <= 0 or counter_amount is None or counter_amount <= 0:
                raise ValueError("FX requires positive source and counter amounts")

        if event_type == TransactionType.REVERSAL and not str(self.reverses_transaction_id or "").strip():
            raise ValueError("REVERSAL requires reverses_transaction_id")

        return replace(
            self,
            event_type=event_type,
            effective_at=effective_at,
            entered_at=entered_at,
            account=account,
            code=str(self.code or "").strip().upper() or None,
            currency=currency,
            counter_currency=counter_currency,
            quantity=quantity,
            price=price,
            fee=fee,
            tax=tax,
            amount=amount,
            counter_amount=counter_amount,
            source=str(self.source or "manual").strip() or "manual",
            note=str(self.note or ""),
            metadata=dict(self.metadata or {}),
        )
