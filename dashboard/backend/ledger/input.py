"""Pure V3 input normalization: date resolution and parsed-change conversion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .models import Transaction, TransactionType


@dataclass(frozen=True)
class ResolvedEffectiveTime:
    effective_at: str
    precision: str  # "date", "minute", "now"
    matched_text: str
    explicit: bool


_DATE_PATTERNS = [
    re.compile(r"(?P<y>20\d{2})[-/.](?P<m>\d{1,2})[-/.](?P<d>\d{1,2})"),
    re.compile(r"(?P<y>20\d{2})年(?P<m>\d{1,2})月(?P<d>\d{1,2})(?:日|号)?"),
]
_MONTH_DAY = re.compile(r"(?:(?P<rel>前年|去年|今年))?(?P<m>\d{1,2})月(?P<d>\d{1,2})(?:日|号)?")
_CLOCK_COLON = re.compile(r"(?<!\d)(?P<h>[01]?\d|2[0-3]):(?P<min>[0-5]\d)(?!\d)")
_CLOCK_CN = re.compile(
    r"(?P<period>凌晨|早上|上午|中午|下午|傍晚|晚上)?\s*"
    r"(?P<h>\d{1,2})\s*点(?:\s*(?P<min>\d{1,2})\s*分?)?"
)


def _ensure_reference(reference: Optional[datetime]) -> datetime:
    if reference is None:
        return datetime.now().astimezone()
    if reference.tzinfo is None:
        return reference.replace(tzinfo=timezone.utc)
    return reference


def _valid_date(year: int, month: int, day: int) -> date:
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise ValueError(f"invalid effective date: {year:04d}-{month:02d}-{day:02d}") from exc


def _parse_clock(text: str) -> Tuple[Optional[time], str]:
    match = _CLOCK_COLON.search(text)
    if match:
        return time(int(match.group("h")), int(match.group("min"))), match.group(0)

    match = _CLOCK_CN.search(text)
    if not match:
        return None, ""
    hour = int(match.group("h"))
    minute = int(match.group("min") or 0)
    if minute > 59:
        raise ValueError("invalid minute in effective time")
    period = match.group("period") or ""
    if hour > 23:
        raise ValueError("invalid hour in effective time")
    if period in {"下午", "傍晚", "晚上"} and 1 <= hour <= 11:
        hour += 12
    elif period == "中午" and 1 <= hour <= 10:
        hour += 12
    elif period in {"凌晨", "早上", "上午"} and hour == 12:
        hour = 0
    return time(hour, minute), match.group(0)


def resolve_effective_time(text: str, *, reference: Optional[datetime] = None) -> ResolvedEffectiveTime:
    """Resolve common Chinese/ISO date expressions without LLM guessing.

    If no date is present, the event is treated as happening now. If a date is
    explicit but time is omitted, midnight is used and precision is recorded as
    ``date`` so callers can show that fact on the confirmation card.
    """
    raw = str(text or "")
    ref = _ensure_reference(reference)
    target_date: Optional[date] = None
    date_match_text = ""

    for pattern in _DATE_PATTERNS:
        match = pattern.search(raw)
        if match:
            target_date = _valid_date(int(match.group("y")), int(match.group("m")), int(match.group("d")))
            date_match_text = match.group(0)
            break

    if target_date is None:
        for token, delta in (("前天", 2), ("昨天", 1), ("今天", 0)):
            if token in raw:
                target_date = (ref - timedelta(days=delta)).date()
                date_match_text = token
                break

    if target_date is None:
        match = _MONTH_DAY.search(raw)
        if match:
            rel = match.group("rel")
            year = ref.year + {"前年": -2, "去年": -1, "今年": 0}.get(rel, 0)
            target_date = _valid_date(year, int(match.group("m")), int(match.group("d")))
            date_match_text = match.group(0)

    clock, clock_text = _parse_clock(raw)
    if target_date is None:
        if clock is None:
            return ResolvedEffectiveTime(ref.isoformat(), "now", "", False)
        target_date = ref.date()

    if clock is None:
        target_time = time.min
        precision = "date"
    else:
        target_time = clock
        precision = "minute"

    resolved = datetime.combine(target_date, target_time, tzinfo=ref.tzinfo)
    matched = " ".join(part for part in (date_match_text, clock_text) if part)
    return ResolvedEffectiveTime(resolved.isoformat(), precision, matched, bool(date_match_text or clock_text))




def strip_effective_time_text(text: str) -> str:
    """Remove deterministic date/time phrases before legacy bookkeeping parsing."""
    cleaned = str(text or "")
    for pattern in _DATE_PATTERNS:
        cleaned = pattern.sub(" ", cleaned)
    cleaned = re.sub(r"(?:前天|昨天|今天)", " ", cleaned)
    cleaned = _MONTH_DAY.sub(" ", cleaned)
    cleaned = _CLOCK_COLON.sub(" ", cleaned)
    cleaned = _CLOCK_CN.sub(" ", cleaned)
    cleaned = cleaned.replace("，", " ")
    cleaned = re.sub(r"(?<!\d),(?!\d)", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned

def _decimal(value: Any, default: Optional[Decimal] = None) -> Optional[Decimal]:
    if value is None or value == "":
        return default
    return Decimal(str(value).replace(",", "").strip())


def transaction_from_change(
    change: Dict[str, Any],
    *,
    effective_at: str,
    entered_at: Optional[str] = None,
    source: str = "text",
    date_precision: Optional[str] = None,
) -> Transaction:
    """Convert one existing bookkeeper change into one V3 economic event."""
    action = str(change.get("action_type") or change.get("action") or "").strip()
    mapping = {
        "add_or_update": TransactionType.BUY,
        "buy": TransactionType.BUY,
        "sell": TransactionType.SELL,
        "set_position": TransactionType.OPENING_POSITION,
        "deposit": TransactionType.DEPOSIT,
        "withdraw": TransactionType.WITHDRAW,
        "set_cash": TransactionType.OPENING_CASH,
    }
    if action not in mapping:
        raise ValueError(f"unsupported bookkeeper action for one-event conversion: {action}")

    event_type = mapping[action]
    metadata = dict(change.get("metadata") or {})
    if date_precision:
        metadata["date_precision"] = date_precision
    if change.get("total_cost") is not None:
        metadata["legacy_total_cost"] = str(change.get("total_cost"))

    kwargs: Dict[str, Any] = {
        "event_type": event_type,
        "effective_at": effective_at,
        "account": str(change.get("account") or change.get("group") or "").strip(),
        "entered_at": entered_at or datetime.now(timezone.utc).isoformat(),
        "source": source,
        "note": str(change.get("note") or ""),
        "external_trade_id": change.get("external_trade_id"),
        "metadata": metadata,
    }

    if event_type in {TransactionType.DEPOSIT, TransactionType.WITHDRAW, TransactionType.OPENING_CASH}:
        kwargs.update(currency=change.get("currency"), amount=_decimal(change.get("amount")))
    else:
        kwargs.update(
            instrument_id=change.get("instrument_id"),
            code=change.get("code") or change.get("symbol"),
            name=change.get("name"),
            currency=change.get("currency"),
            quantity=_decimal(change.get("quantity")),
            price=_decimal(change.get("cost_price") if change.get("cost_price") is not None else change.get("price")),
            fee=_decimal(change.get("fee"), Decimal("0")) or Decimal("0"),
            tax=_decimal(change.get("tax"), Decimal("0")) or Decimal("0"),
        )
    return Transaction(**kwargs).validated()


def fx_transaction_from_changes(
    changes: Sequence[Dict[str, Any]],
    *,
    effective_at: str,
    entered_at: Optional[str] = None,
    source: str = "text",
    date_precision: Optional[str] = None,
) -> Transaction:
    """Convert the existing withdraw+deposit FX representation into one event."""
    if len(changes) != 2:
        raise ValueError("FX conversion requires exactly two cash changes")
    source_change = next((c for c in changes if str(c.get("action_type")) == "withdraw"), None)
    target_change = next((c for c in changes if str(c.get("action_type")) == "deposit"), None)
    if source_change is None or target_change is None:
        raise ValueError("FX conversion requires withdraw then deposit semantics")
    source_account = str(source_change.get("account") or "").strip()
    target_account = str(target_change.get("account") or "").strip()
    if source_account != target_account:
        raise ValueError("FX source and target must use the same account")
    metadata = {"date_precision": date_precision} if date_precision else {}
    return Transaction(
        event_type=TransactionType.FX,
        effective_at=effective_at,
        entered_at=entered_at or datetime.now(timezone.utc).isoformat(),
        account=source_account,
        currency=source_change.get("currency"),
        amount=_decimal(source_change.get("amount")),
        counter_currency=target_change.get("currency"),
        counter_amount=_decimal(target_change.get("amount")),
        source=source,
        metadata=metadata,
    ).validated()
