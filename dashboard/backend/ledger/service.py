"""Semantic write service for the V3 transaction ledger."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional
from uuid import uuid4

from .models import Transaction
from .replay import ReplayState, replay_transactions
from .repository import TransactionRepository


@dataclass(frozen=True)
class LedgerPreview:
    events: List[Transaction]
    projected_state: ReplayState
    historical_backfill: bool


class LedgerWriteService:
    """Validate economic history before durable append.

    Repository-level constraints protect storage integrity. This service adds
    semantic integrity: a historical insertion is accepted only if replaying
    the complete resulting history succeeds (no oversell, invalid cash path,
    conflicting opening baseline, etc.).
    """

    def __init__(self, repository: Optional[TransactionRepository] = None):
        self.repository = repository or TransactionRepository()

    def preview(self, events: Iterable[Transaction]) -> LedgerPreview:
        proposed = [event.validated() for event in events]
        existing = self.repository.list_transactions()
        projected = replay_transactions([*existing, *proposed])
        latest_existing_effective = max((str(tx.effective_at) for tx in existing), default="")
        historical_backfill = bool(
            latest_existing_effective
            and any(str(event.effective_at) < latest_existing_effective for event in proposed)
        )
        return LedgerPreview(
            events=proposed,
            projected_state=projected,
            historical_backfill=historical_backfill,
        )

    def confirm(self, events: Iterable[Transaction]) -> LedgerPreview:
        proposed = [event.validated() for event in events]
        result = {}

        def validate_locked(existing: List[Transaction], locked_proposed: List[Transaction]) -> None:
            projected = replay_transactions([*existing, *locked_proposed])
            latest_existing_effective = max((str(tx.effective_at) for tx in existing), default="")
            historical_backfill = bool(
                latest_existing_effective
                and any(str(event.effective_at) < latest_existing_effective for event in locked_proposed)
            )
            result["preview"] = LedgerPreview(
                events=list(locked_proposed),
                projected_state=projected,
                historical_backfill=historical_backfill,
            )

        self.repository.append_many_atomic(proposed, precommit_validator=validate_locked)
        return result["preview"]
    def create_pending(self, events: Iterable[Transaction], *, ttl_seconds: int = 1800) -> dict:
        preview = self.preview(events)
        pending_id = str(uuid4())
        stored = self.repository.create_pending_batch(
            pending_id, preview.events, ttl_seconds=ttl_seconds
        )
        return {
            **stored,
            "projected_state": preview.projected_state,
            "historical_backfill": preview.historical_backfill,
        }

    def confirm_pending(self, pending_id: str) -> LedgerPreview:
        def validate(existing: List[Transaction], proposed: List[Transaction]) -> LedgerPreview:
            projected = replay_transactions([*existing, *proposed])
            latest_existing_effective = max((str(tx.effective_at) for tx in existing), default="")
            historical_backfill = bool(
                latest_existing_effective
                and any(str(event.effective_at) < latest_existing_effective for event in proposed)
            )
            return LedgerPreview(
                events=list(proposed),
                projected_state=projected,
                historical_backfill=historical_backfill,
            )

        _events, preview = self.repository.confirm_pending_batch(pending_id, validate)
        return preview
