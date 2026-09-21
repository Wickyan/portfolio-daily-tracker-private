"""HTTP API for the V3 transaction ledger."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from backend.ledger import (
    LedgerWriteService,
    Transaction,
    TransactionRepository,
    TransactionType,
    parse_historical_bookkeeping_text,
    replay_state_to_portfolio,
    replay_transactions,
    ScreenshotLedgerExtractor,
    create_configured_vision_providers,
    create_local_ocr_fallback,
    transaction_fingerprint_key,
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


class LedgerReverseRequest(BaseModel):
    reason: str = Field(default="", max_length=1000)
    ttl_seconds: int = Field(default=1800, ge=30, le=3600)


def get_repository() -> TransactionRepository:
    return TransactionRepository(Path("data") / "ledger.sqlite3")


def get_write_service() -> LedgerWriteService:
    return LedgerWriteService(get_repository())


def _decimal_text(value):
    return None if value is None else str(value)


def _transaction_payload(tx, *, is_reversed: bool = False, reversed_by: Optional[str] = None):
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
        "is_reversed": is_reversed,
        "reversed_by": reversed_by,
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
        "reversed_transaction_ids": list(state.reversed_transaction_ids),
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
    all_rows = get_repository().list_transactions()
    reversed_by = {
        str(tx.reverses_transaction_id): tx.transaction_id
        for tx in all_rows
        if tx.event_type == TransactionType.REVERSAL and tx.reverses_transaction_id
    }
    return {
        "count": len(rows),
        "transactions": [
            _transaction_payload(
                tx,
                is_reversed=tx.transaction_id in reversed_by,
                reversed_by=reversed_by.get(tx.transaction_id),
            )
            for tx in rows
        ],
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


def _screenshot_candidate_payload(candidate):
    return {
        "event": _transaction_payload(candidate.event) if candidate.event is not None else None,
        "raw": candidate.raw,
        "warnings": list(candidate.warnings),
        "missing_fields": list(candidate.missing_fields),
        "duplicate_reason": candidate.duplicate_reason,
    }


@router.post("/preview-screenshots")
async def preview_screenshots(
    images: List[UploadFile] = File(...),
    account_hint: str = Form(""),
    ttl_seconds: int = Form(1800),
):
    if not images:
        raise HTTPException(status_code=400, detail="至少需要一张截图")
    if len(images) > 12:
        raise HTTPException(status_code=400, detail="一次最多上传12张截图")
    if ttl_seconds < 30 or ttl_seconds > 3600:
        raise HTTPException(status_code=400, detail="ttl_seconds must be between 30 and 3600")

    payloads = []
    total_bytes = 0
    for image in images:
        if image.content_type and not image.content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail=f"{image.filename or 'file'} 不是图片")
        content = await image.read()
        if len(content) > 50 * 1024 * 1024:
            raise HTTPException(
                status_code=413,
                detail=f"{image.filename or 'file'} 超过单张50MB限制",
            )
        total_bytes += len(content)
        if total_bytes > 150 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="本次截图总大小超过150MB")
        payloads.append((image.filename or "screenshot", content))

    vision = []
    vision_error = None
    try:
        vision = create_configured_vision_providers()
    except RuntimeError as exc:
        vision_error = str(exc)

    ocr_fallback = None
    ocr_error = None
    try:
        ocr_fallback = create_local_ocr_fallback()
    except RuntimeError as exc:
        ocr_error = str(exc)

    if not vision and ocr_fallback is None:
        detail = "截图识别能力不可用"
        if vision_error:
            detail += f"；视觉模型: {vision_error}"
        if ocr_error:
            detail += f"；本地OCR: {ocr_error}"
        raise HTTPException(status_code=503, detail=detail)

    repository = get_repository()
    existing_rows = repository.list_transactions()
    pending_rows = repository.list_active_pending_transactions()
    all_existing_rows = [*existing_rows, *pending_rows]
    existing_keys = {
        (tx.account, str(tx.external_trade_id))
        for tx in all_existing_rows
        if tx.external_trade_id
    }
    existing_fingerprint_keys = {
        (tx.account, transaction_fingerprint_key(tx))
        for tx in all_existing_rows
        if not tx.external_trade_id
    }

    extractor = ScreenshotLedgerExtractor(vision, ocr_fallback=ocr_fallback)
    try:
        result = await extractor.extract(
            payloads,
            account_hint=account_hint.strip(),
            existing_external_keys=existing_keys,
            existing_fingerprint_keys=existing_fingerprint_keys,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=f"截图识别失败: {exc}") from exc

    pending = None
    if result.events:
        try:
            pending = get_write_service().create_pending(
                result.events,
                ttl_seconds=ttl_seconds,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    if pending is not None:
        response = {
            "pending_id": pending["pending_id"],
            "status": pending["status"],
            "created_at": pending["created_at"],
            "expires_at": pending["expires_at"],
            "historical_backfill": pending["historical_backfill"],
            "events": [_transaction_payload(tx) for tx in pending["events"]],
            "projected_state": _state_payload(pending["projected_state"]),
        }
    else:
        current_state = replay_transactions(existing_rows)
        response = {
            "pending_id": None,
            "status": "no_new_events",
            "created_at": None,
            "expires_at": None,
            "historical_backfill": False,
            "events": [],
            "projected_state": _state_payload(current_state),
        }

    response["extraction"] = {
        "model": result.model_label,
        "images": result.image_summaries,
        "warnings": result.warnings,
        "duplicate_count": len(result.duplicates),
        "duplicates": [_screenshot_candidate_payload(item) for item in result.duplicates],
        "unresolved_count": len(result.unresolved),
        "unresolved": [_screenshot_candidate_payload(item) for item in result.unresolved],
    }
    return response


@router.post("/reverse/{transaction_id}/preview")
async def preview_reversal(transaction_id: str, request: LedgerReverseRequest):
    repository = get_repository()
    target = repository.get(transaction_id)
    if target is None:
        raise HTTPException(status_code=404, detail="transaction not found")
    if target.event_type == TransactionType.REVERSAL:
        raise HTTPException(status_code=409, detail="reversal of a reversal is not supported")

    reversal = Transaction(
        event_type=TransactionType.REVERSAL,
        effective_at=target.effective_at,
        account=target.account,
        source="manual",
        note=request.reason.strip(),
        reverses_transaction_id=target.transaction_id,
        metadata={
            "target_event_type": target.event_type.value,
            "target_code": target.code,
            "target_effective_at": str(target.effective_at),
        },
    ).validated()
    try:
        pending = LedgerWriteService(repository).create_pending(
            [reversal], ttl_seconds=request.ttl_seconds
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
