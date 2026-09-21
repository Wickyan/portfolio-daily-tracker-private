"""V3 transaction-ledger primitives.

The ledger is intentionally isolated from the existing portfolio write path.
Current holdings remain untouched until the replay layer is introduced.
"""

from .models import Transaction, TransactionType
from .repository import TransactionRepository
from .replay import PositionState, ReplayError, ReplayState, replay_transactions
from .compat import replay_state_to_portfolio
from .input import ResolvedEffectiveTime, fx_transaction_from_changes, resolve_effective_time, strip_effective_time_text, transaction_from_change
from .service import LedgerPreview, LedgerWriteService

__all__ = [
    "Transaction", "TransactionType", "TransactionRepository",
    "PositionState", "ReplayState", "ReplayError", "replay_transactions",
    "replay_state_to_portfolio",
    "ResolvedEffectiveTime", "resolve_effective_time", "strip_effective_time_text",
    "transaction_from_change", "fx_transaction_from_changes",
    "LedgerPreview", "LedgerWriteService",
    "ScreenshotLedgerExtractor", "ScreenshotExtractionResult", "create_configured_vision_provider", "create_configured_vision_providers", "create_local_ocr_fallback", "prepare_image_tiles", "transaction_fingerprint_key",
    "HistoricalTextParseResult", "TextClauseInterpretation", "parse_historical_bookkeeping_text",
]

from .text_adapter import HistoricalTextParseResult, TextClauseInterpretation, parse_historical_bookkeeping_text

from .screenshot import ScreenshotLedgerExtractor, ScreenshotExtractionResult, create_configured_vision_provider, create_configured_vision_providers, create_local_ocr_fallback, prepare_image_tiles, transaction_fingerprint_key
