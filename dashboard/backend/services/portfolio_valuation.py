"""Pure portfolio valuation helpers used by API code and tests."""
from __future__ import annotations

from typing import Any, Dict

from backend.services.portfolio_write_service import to_float


def calculate_portfolio_valuation(
    raw_portfolio: Dict[str, Any],
    fx_rates: Dict[str, float],
) -> Dict[str, Any]:
    rates = {str(k).upper(): float(v) for k, v in fx_rates.items() if float(v) > 0}
    rates.setdefault("CNY", 1.0)

    positions = []
    currency_totals: Dict[str, Dict[str, float]] = {}
    total_market_value_cny = 0.0
    total_profit_cny = 0.0

    for raw in raw_portfolio.get("positions", []):
        quantity = to_float(raw.get("quantity"), 0.0) or 0.0
        cost_price = to_float(raw.get("cost_price"), 0.0) or 0.0
        current_price = to_float(raw.get("current_price"), cost_price) or cost_price
        currency = str(raw.get("currency") or "CNY").upper()
        fx_rate = rates.get(currency)
        market_value = quantity * current_price
        profit = market_value - quantity * cost_price
        market_value_cny = market_value * fx_rate if fx_rate is not None else None
        profit_cny = profit * fx_rate if fx_rate is not None else None
        profit_pct = (profit / (quantity * cost_price) * 100) if quantity * cost_price > 0 else 0.0

        bucket = currency_totals.setdefault(currency, {
            "market_value": 0.0,
            "cash": 0.0,
            "profit": 0.0,
            "market_value_cny": 0.0,
            "cash_cny": 0.0,
            "profit_cny": 0.0,
        })
        bucket["market_value"] += market_value
        bucket["profit"] += profit
        if market_value_cny is not None:
            bucket["market_value_cny"] += market_value_cny
            bucket["profit_cny"] += profit_cny or 0.0
            total_market_value_cny += market_value_cny
            total_profit_cny += profit_cny or 0.0

        positions.append({
            "account": raw.get("account", ""),
            "code": raw.get("code", ""),
            "name": raw.get("name", ""),
            "currency": currency,
            "asset_type": raw.get("asset_type", "stock"),
            "quantity": quantity,
            "available_qty": to_float(raw.get("available_qty"), quantity) or quantity,
            "cost_price": cost_price,
            "current_price": current_price,
            "total_cost": to_float(raw.get("total_cost"), quantity * cost_price) or quantity * cost_price,
            "fee": to_float(raw.get("fee"), None),
            "note": raw.get("note", ""),
            "source": raw.get("source", "manual"),
            "profit": profit,
            "profit_cny": profit_cny,
            "profit_pct": profit_pct,
            "market_value": market_value,
            "market_value_cny": market_value_cny,
            "fx_rate": fx_rate,
        })

    cash_accounts = []
    total_cash_cny = to_float(raw_portfolio.get("cash"), 0.0) or 0.0
    if total_cash_cny:
        bucket = currency_totals.setdefault("CNY", {
            "market_value": 0.0, "cash": 0.0, "profit": 0.0,
            "market_value_cny": 0.0, "cash_cny": 0.0, "profit_cny": 0.0,
        })
        bucket["cash"] += total_cash_cny
        bucket["cash_cny"] += total_cash_cny

    for raw in raw_portfolio.get("cash_accounts", []):
        account = str(raw.get("account") or "").strip()
        currency = str(raw.get("currency") or "CNY").upper()
        amount = to_float(raw.get("amount"), 0.0) or 0.0
        fx_rate = rates.get(currency)
        amount_cny = amount * fx_rate if fx_rate is not None else None
        bucket = currency_totals.setdefault(currency, {
            "market_value": 0.0, "cash": 0.0, "profit": 0.0,
            "market_value_cny": 0.0, "cash_cny": 0.0, "profit_cny": 0.0,
        })
        bucket["cash"] += amount
        if amount_cny is not None:
            bucket["cash_cny"] += amount_cny
            total_cash_cny += amount_cny
        cash_accounts.append({
            "account": account,
            "currency": currency,
            "amount": amount,
            "fx_rate": fx_rate,
            "amount_cny": amount_cny,
            "updated_at": raw.get("updated_at"),
        })

    return {
        "positions": positions,
        "cash_accounts": cash_accounts,
        "cash": total_cash_cny,
        "legacy_cash_cny": to_float(raw_portfolio.get("cash"), 0.0) or 0.0,
        "base_currency": "CNY",
        "fx_rates": rates,
        "currency_totals": currency_totals,
        "total_market_value": total_market_value_cny,
        "total_assets": total_market_value_cny + total_cash_cny,
        "total_profit": total_profit_cny,
    }
