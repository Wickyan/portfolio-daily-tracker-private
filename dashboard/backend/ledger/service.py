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

        self.repository.backup_database()
        self.repository.append_many_atomic(proposed, precommit_validator=validate_locked)
        return result["preview"]
    def create_pending(
        self,
        events: Iterable[Transaction],
        *,
        ttl_seconds: int = 1800,
        allow_incomplete_history: bool = False,
    ) -> dict:
        proposed = [event.validated() for event in events]
        replay_error = None
        confirm_blocked = False
        try:
            preview = self.preview(proposed)
        except ValueError as exc:
            if not allow_incomplete_history:
                raise
            existing = self.repository.list_transactions()
            # Existing durable history must itself remain replayable. Only the
            # newly staged historical batch is allowed to depend on missing
            # pre-history/opening positions.
            current_state = replay_transactions(existing)
            latest_existing_effective = max(
                (str(tx.effective_at) for tx in existing),
                default="",
            )
            historical_backfill = bool(
                latest_existing_effective
                and any(str(event.effective_at) < latest_existing_effective for event in proposed)
            )
            preview = LedgerPreview(
                events=proposed,
                projected_state=current_state,
                historical_backfill=historical_backfill or bool(proposed),
            )
            replay_error = str(exc)
            confirm_blocked = True

        if confirm_blocked:
            # A batch that cannot yet replay is a recognition preview, not a
            # durable pending write. Keeping it out of pending_batches prevents
            # invisible/stale previews from suppressing a later re-upload.
            return {
                "pending_id": None,
                "status": "replay_blocked",
                "created_at": None,
                "expires_at": None,
                "events": preview.events,
                "projected_state": preview.projected_state,
                "historical_backfill": preview.historical_backfill,
                "confirm_blocked": True,
                "replay_error": replay_error,
            }

        pending_id = str(uuid4())
        stored = self.repository.create_pending_batch(
            pending_id, preview.events, ttl_seconds=ttl_seconds
        )
        return {
            **stored,
            "projected_state": preview.projected_state,
            "historical_backfill": preview.historical_backfill,
            "confirm_blocked": False,
            "replay_error": None,
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

        self.repository.backup_database()
        _events, preview = self.repository.confirm_pending_batch(pending_id, validate)
        return preview
