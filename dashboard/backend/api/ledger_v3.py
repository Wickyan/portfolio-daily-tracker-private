"""HTTP API for the V3 transaction ledger."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from backend.ledger import (
    LedgerWriteService,
    Transaction,
    TransactionRepository,
    TransactionType,
    parse_historical_bookkeeping_text,
    replay_state_to_portfolio,
    replay_transactions,
)


router = APIRouter()
NumberInput = Union[str, int, float]


class LedgerEventRequest(BaseModel):
    event_type: str
    effective_at: str
    account: str

    entered_at: Optional[str] = None
    instrument_id: Optional[str] = None
    code: Optional[str] = None
    name: Optional[str] = None
    currency: Optional[str] = None
    quantity: Optional[NumberInput] = None
    price: Optional[NumberInput] = None
    fee: NumberInput = "0"
    tax: NumberInput = "0"
    amount: Optional[NumberInput] = None
    counter_currency: Optional[str] = None
    counter_amount: Optional[NumberInput] = None
    source: str = "manual"
    note: str = ""
    external_trade_id: Optional[str] = None
    reverses_transaction_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class LedgerPreviewRequest(BaseModel):
    events: List[LedgerEventRequest]
    ttl_seconds: int = Field(default=1800, ge=30, le=3600)


class LedgerTextPreviewRequest(BaseModel):
    message: str = Field(min_length=1, max_length=10000)
    reference_time: Optional[str] = None
    source: str = "text"
    ttl_seconds: int = Field(default=1800, ge=30, le=3600)


def get_repository() -> TransactionRepository:
    return TransactionRepository(Path("data") / "ledger.sqlite3")


def get_write_service() -> LedgerWriteService:
    return LedgerWriteService(get_repository())


def _decimal_text(value):
    return None if value is None else str(value)


def _transaction_payload(tx):
    return {
        "transaction_id": tx.transaction_id,
        "event_type": tx.event_type.value,
        "effective_at": str(tx.effective_at),
        "entered_at": str(tx.entered_at),
        "account": tx.account,
        "instrument_id": tx.instrument_id,
        "code": tx.code,
        "name": tx.name,
        "currency": tx.currency,
        "quantity": _decimal_text(tx.quantity),
        "price": _decimal_text(tx.price),
        "fee": _decimal_text(tx.fee),
        "tax": _decimal_text(tx.tax),
        "amount": _decimal_text(tx.amount),
        "counter_currency": tx.counter_currency,
        "counter_amount": _decimal_text(tx.counter_amount),
        "source": tx.source,
        "note": tx.note,
        "external_trade_id": tx.external_trade_id,
        "reverses_transaction_id": tx.reverses_transaction_id,
        "metadata": tx.metadata,
    }


def _state_payload(state):
    positions = []
    for key in sorted(state.positions):
        pos = state.positions[key]
        positions.append({
            "account": pos.account,
            "code": pos.code,
            "name": pos.name,
            "currency": pos.currency,
            "quantity": str(pos.quantity),
            "total_cost": str(pos.total_cost),
            "average_cost": str(pos.average_cost),
            "realized_pnl": str(state.realized_pnl.get(key, Decimal("0"))),
            "dividend_income": str(state.dividend_income.get(key, Decimal("0"))),
        })
    cash = [
        {"account": account, "currency": currency, "amount": str(amount)}
        for (account, currency), amount in sorted(state.cash.items())
    ]
    return {
        "positions": positions,
        "cash_accounts": cash,
        "applied_transactions": len(state.applied_transaction_ids),
    }


def _request_to_transaction(request: LedgerEventRequest) -> Transaction:
    try:
        event_type = TransactionType(request.event_type.upper())
    except ValueError as exc:
        raise ValueError(f"unsupported event_type: {request.event_type}") from exc

    kwargs = request.model_dump()
    kwargs["event_type"] = event_type
    if kwargs.get("entered_at") is None:
        kwargs.pop("entered_at", None)
    return Transaction(**kwargs).validated()


@router.get("/transactions")
async def list_transactions(
    account: Optional[str] = None,
    code: Optional[str] = None,
    effective_from: Optional[str] = Query(None, alias="from"),
    effective_to: Optional[str] = Query(None, alias="to"),
):
    try:
        rows = get_repository().list_transactions(
            account=account,
            code=code,
            effective_from=effective_from,
            effective_to=effective_to,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "count": len(rows),
        "transactions": [_transaction_payload(tx) for tx in rows],
    }


@router.get("/state")
async def get_state(as_of: Optional[str] = None):
    rows = get_repository().list_transactions()
    try:
        state = replay_transactions(rows, as_of=as_of)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "as_of": as_of,
        **_state_payload(state),
    }


@router.get("/portfolio-compat")
async def get_portfolio_compat(as_of: Optional[str] = None):
    rows = get_repository().list_transactions()
    try:
        state = replay_transactions(rows, as_of=as_of)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return replay_state_to_portfolio(state)


@router.post("/preview")
async def preview_events(request: LedgerPreviewRequest):
    try:
        events = [_request_to_transaction(item) for item in request.events]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        pending = get_write_service().create_pending(
            events,
            ttl_seconds=request.ttl_seconds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {
        "pending_id": pending["pending_id"],
        "status": pending["status"],
        "created_at": pending["created_at"],
        "expires_at": pending["expires_at"],
        "historical_backfill": pending["historical_backfill"],
        "events": [_transaction_payload(tx) for tx in pending["events"]],
        "projected_state": _state_payload(pending["projected_state"]),
    }


@router.post("/preview-text")
async def preview_text(request: LedgerTextPreviewRequest):
    reference = None
    if request.reference_time:
        raw_reference = request.reference_time
        candidate = raw_reference[:-1] + "+00:00" if raw_reference.endswith("Z") else raw_reference
        try:
            reference = datetime.fromisoformat(candidate)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="reference_time must be ISO datetime") from exc
        if reference.tzinfo is None:
            raise HTTPException(status_code=400, detail="reference_time must include timezone offset")

    if request.source not in {"text", "voice"}:
        raise HTTPException(status_code=400, detail="source must be text or voice")

    try:
        interpreted = parse_historical_bookkeeping_text(
            request.message, reference=reference, source=request.source
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        pending = get_write_service().create_pending(
            interpreted.events, ttl_seconds=request.ttl_seconds
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {
        "pending_id": pending["pending_id"],
        "status": pending["status"],
        "created_at": pending["created_at"],
        "expires_at": pending["expires_at"],
        "historical_backfill": pending["historical_backfill"],
        "events": [_transaction_payload(tx) for tx in pending["events"]],
        "projected_state": _state_payload(pending["projected_state"]),
        "interpretation": {
            "warnings": interpreted.warnings,
            "clauses": [
                {
                    "original": item.original,
                    "cleaned": item.cleaned,
                    "effective_at": item.effective_at,
                    "date_precision": item.date_precision,
                    "action_type": item.action_type,
                    "warnings": item.warnings,
                }
                for item in interpreted.clauses
            ],
        },
    }


@router.get("/pending/{pending_id}")
async def get_pending(pending_id: str):
    pending = get_repository().get_pending_batch(pending_id)
    if pending is None:
        raise HTTPException(status_code=404, detail="pending batch not found")
    return {
        "pending_id": pending["pending_id"],
        "status": pending["status"],
        "created_at": pending["created_at"],
        "expires_at": pending["expires_at"],
        "confirmed_at": pending["confirmed_at"],
        "events": [_transaction_payload(tx) for tx in pending["events"]],
    }


@router.post("/confirm/{pending_id}")
async def confirm_pending(pending_id: str):
    service = get_write_service()
    try:
        preview = service.confirm_pending(pending_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="pending batch not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    pending = get_repository().get_pending_batch(pending_id)
    return {
        "pending_id": pending_id,
        "status": pending["status"] if pending else "confirmed",
        "historical_backfill": preview.historical_backfill,
        "events": [_transaction_payload(tx) for tx in preview.events],
        "projected_state": _state_payload(preview.projected_state),
    }
