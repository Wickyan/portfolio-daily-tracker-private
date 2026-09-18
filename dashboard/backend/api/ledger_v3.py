"""Read-only HTTP API for the V3 transaction ledger."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from backend.ledger import TransactionRepository, replay_state_to_portfolio, replay_transactions


router = APIRouter()


def get_repository() -> TransactionRepository:
    return TransactionRepository(Path("data") / "ledger.sqlite3")


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


@router.get("/transactions")
async def list_transactions(
    account: Optional[str] = None,
    code: Optional[str] = None,
    effective_from: Optional[str] = Query(None, alias="from"),
    effective_to: Optional[str] = Query(None, alias="to"),
):
    rows = get_repository().list_transactions(
        account=account,
        code=code,
        effective_from=effective_from,
        effective_to=effective_to,
    )
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
    state = replay_transactions(rows, as_of=as_of)
    return replay_state_to_portfolio(state)
