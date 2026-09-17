"""Read-only compatibility adapter from V3 replay state to legacy portfolio shape."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Mapping, Optional, Tuple

from .replay import PositionKey, ReplayState


QuoteKey = Tuple[str, str, str]


def decimal_to_float(value: Decimal) -> float:
    """Convert only at the legacy/dashboard boundary.

    The V3 ledger/replay core stays Decimal-exact. The existing valuation/UI
    layer is float-based, so loss of representation is deliberately isolated
    here instead of leaking back into the ledger.
    """
    return float(value)


def replay_state_to_portfolio(
    state: ReplayState,
    *,
    current_prices: Optional[Mapping[QuoteKey, Decimal]] = None,
    generated_at: Optional[str] = None,
) -> Dict:
    """Build the legacy raw-portfolio shape without mutating any old files."""
    prices = current_prices or {}
    positions = []

    for key in sorted(state.positions):
        position = state.positions[key]
        current_price = prices.get(key, position.average_cost)
        realized = state.realized_pnl.get(key, Decimal("0"))
        dividend = state.dividend_income.get(key, Decimal("0"))
        positions.append({
            "account": position.account,
            "name": position.name,
            "code": position.code,
            "currency": position.currency,
            "asset_type": "stock",
            "quantity": decimal_to_float(position.quantity),
            "available_qty": decimal_to_float(position.quantity),
            "cost_price": decimal_to_float(position.average_cost),
            "total_cost": decimal_to_float(position.total_cost),
            "current_price": decimal_to_float(current_price),
            "fee": None,
            "note": "",
            "source": "ledger-v3",
            "realized_pnl": decimal_to_float(realized),
            "dividend_income": decimal_to_float(dividend),
        })

    cash_accounts = []
    for (account, currency), amount in sorted(state.cash.items()):
        cash_accounts.append({
            "account": account,
            "currency": currency,
            "amount": decimal_to_float(amount),
            "updated_at": generated_at,
        })

    return {
        "positions": positions,
        "cash": 0.0,
        "cash_accounts": cash_accounts,
        "updated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "ledger_v3": {
            "applied_transactions": len(state.applied_transaction_ids),
            "realized_pnl": {
                "|".join(key): decimal_to_float(value)
                for key, value in sorted(state.realized_pnl.items())
            },
            "dividend_income": {
                "|".join(key): decimal_to_float(value)
                for key, value in sorted(state.dividend_income.items())
            },
        },
    }
