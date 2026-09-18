"""Adapter from natural-language bookkeeping text to V3 transaction events.

This layer deliberately reuses the mature legacy instrument/account parser, but
removes historical date/time text before parsing and never trusts legacy
current-holding availability checks. Economic validity is decided by V3 replay.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import re
from typing import Any, Dict, List, Optional

from .input import (
    ResolvedEffectiveTime,
    fx_transaction_from_changes,
    resolve_effective_time,
    strip_effective_time_text,
    transaction_from_change,
)
from .models import Transaction


_NUMBER = r"\d+(?:,\d{3})*(?:\.\d+)?"
_FEE_RE = re.compile(
    rf"(?:手续费|佣金|费用)\s*(?:是|为|[:：])?\s*(?P<value>{_NUMBER})",
    re.IGNORECASE,
)
_TAX_RE = re.compile(
    rf"(?:印花税|税费|税)\s*(?:是|为|[:：])?\s*(?P<value>{_NUMBER})",
    re.IGNORECASE,
)
_EXPLICIT_PRICE_RE = re.compile(
    rf"(?:成交价|买入价|卖出价|均价|成本价|价格)\s*(?:是|为|[:：])?\s*(?P<value>{_NUMBER})",
    re.IGNORECASE,
)
_MONEY_PRICE_RE = re.compile(
    rf"(?P<value>{_NUMBER})\s*(?:美元|美金|港币|港元|人民币|元)"
    r"(?:\s*(?:一股|每股|/股))?",
    re.IGNORECASE,
)
_SPLITTER = re.compile(r"(?:[；;\n]+|\s*(?:然后|接着|随后)\s*)")
_POSITION_ACTIONS = {"add_or_update", "buy", "sell", "set_position"}
_LEGACY_AVAILABILITY_BLOCKERS = {
    "existing_position",
    "available_quantity",
    "available_cash",
}


@dataclass(frozen=True)
class TextClauseInterpretation:
    original: str
    cleaned: str
    effective_at: str
    date_precision: str
    action_type: str
    warnings: List[str]


@dataclass(frozen=True)
class HistoricalTextParseResult:
    events: List[Transaction]
    clauses: List[TextClauseInterpretation]
    warnings: List[str]


def _decimal_token(token: Optional[str]) -> Optional[Decimal]:
    if token is None:
        return None
    return Decimal(token.replace(",", ""))


def _extract_fee_tax_price(text: str) -> tuple[Optional[Decimal], Optional[Decimal], Optional[Decimal]]:
    fee_match = _FEE_RE.search(text)
    tax_match = _TAX_RE.search(text)
    fee = _decimal_token(fee_match.group("value")) if fee_match else None
    tax = _decimal_token(tax_match.group("value")) if tax_match else None

    price_source = text
    if fee_match:
        price_source = price_source[:fee_match.start()] + " " + price_source[fee_match.end():]
    tax_match_after = _TAX_RE.search(price_source)
    if tax_match_after:
        price_source = (
            price_source[:tax_match_after.start()]
            + " "
            + price_source[tax_match_after.end():]
        )

    explicit = _EXPLICIT_PRICE_RE.search(price_source)
    if explicit:
        return fee, tax, _decimal_token(explicit.group("value"))

    money_values = [
        _decimal_token(match.group("value"))
        for match in _MONEY_PRICE_RE.finditer(price_source)
    ]
    return fee, tax, money_values[-1] if money_values else None


def _split_clauses(message: str) -> List[str]:
    return [part.strip(" ，,。") for part in _SPLITTER.split(str(message or "")) if part.strip(" ，,。")]


def _reference_from_effective(effective_at: str) -> datetime:
    candidate = effective_at[:-1] + "+00:00" if effective_at.endswith("Z") else effective_at
    return datetime.fromisoformat(candidate)


def parse_historical_bookkeeping_text(
    message: str,
    *,
    reference: Optional[datetime] = None,
    source: str = "text",
) -> HistoricalTextParseResult:
    """Parse one or more historical bookkeeping clauses into V3 events.

    The legacy parser is used for mature account/instrument/action recognition.
    Date semantics, inherited context, trade price/fee extraction, and final
    economic validation are owned by V3.
    """
    from backend.api.portfolio_ai import infer_account, parse_bookkeeping_message

    clauses = _split_clauses(message)
    if not clauses:
        raise ValueError("empty bookkeeping message")

    events: List[Transaction] = []
    interpretations: List[TextClauseInterpretation] = []
    all_warnings: List[str] = []

    context_reference = reference
    previous_effective_at: Optional[str] = None
    previous_account: Optional[str] = None
    previous_asset: Dict[str, Any] = {}

    for index, original in enumerate(clauses):
        resolved = resolve_effective_time(original, reference=context_reference)
        if index > 0 and not resolved.explicit and previous_effective_at:
            effective_at = previous_effective_at
            date_precision = "inherited"
        else:
            effective_at = resolved.effective_at
            date_precision = resolved.precision
        context_reference = _reference_from_effective(effective_at)
        previous_effective_at = effective_at

        cleaned = strip_effective_time_text(original)
        if not cleaned:
            raise ValueError(f"第{index + 1}条只识别到日期/时间，没有交易内容")

        parsed = parse_bookkeeping_message(cleaned)
        if parsed.get("intent") != "bookkeeping":
            raise ValueError(f"第{index + 1}条没有识别为记账操作")
        action = str(parsed.get("action_type") or "")
        if action in {"multiple_operations", "multiple_records_incomplete", "chat_only"}:
            raise ValueError(f"第{index + 1}条需要拆分或补充后再记录")

        warnings = [
            str(item)
            for item in parsed.get("warnings", [])
            if not any(
                marker in str(item)
                for marker in ("暂不支持卖空", "超过账户", "现金不足")
            )
        ]
        missing = [
            str(item)
            for item in parsed.get("missing_fields", [])
            if str(item) not in _LEGACY_AVAILABILITY_BLOCKERS
        ]

        parsed_changes = [dict(change) for change in parsed.get("changes", [])]
        if not parsed_changes:
            raise ValueError(f"第{index + 1}条没有生成可记录字段")

        explicit_account, account_source = infer_account(cleaned)
        explicit_account = explicit_account if account_source in {"explicit", "candidate"} else None

        if action == "fx_exchange":
            for change in parsed_changes:
                if explicit_account:
                    change["account"] = explicit_account
                elif previous_account and not change.get("account"):
                    change["account"] = previous_account
            event = fx_transaction_from_changes(
                parsed_changes,
                effective_at=effective_at,
                source=source,
                date_precision=date_precision,
            )
            previous_account = event.account
            events.append(event)
            interpretations.append(TextClauseInterpretation(
                original=original,
                cleaned=cleaned,
                effective_at=effective_at,
                date_precision=date_precision,
                action_type=action,
                warnings=warnings,
            ))
            all_warnings.extend(warnings)
            continue

        fee, tax, extracted_price = _extract_fee_tax_price(cleaned)

        for change in parsed_changes:
            child_action = str(change.get("action_type") or action)

            if explicit_account:
                change["account"] = explicit_account
            elif previous_account:
                change["account"] = previous_account
                if not any("账户" in warning and "沿用" in warning for warning in warnings):
                    warnings.append(f"第{index + 1}条账户沿用上一条：{previous_account}")
            elif child_action == "sell":
                # Never trust an account inferred only from today's legacy holdings.
                change.pop("account", None)

            if child_action in _POSITION_ACTIONS:
                if not change.get("code") and previous_asset.get("code"):
                    change["code"] = previous_asset["code"]
                    change["name"] = previous_asset.get("name")
                    change["currency"] = change.get("currency") or previous_asset.get("currency")
                    change["asset_type"] = "stock"
                    warnings.append(
                        f"第{index + 1}条标的沿用上一条：{previous_asset['code']}"
                    )

                if extracted_price is not None:
                    change["cost_price"] = extracted_price
                    quantity = change.get("quantity")
                    if quantity is not None:
                        change["total_cost"] = (
                            Decimal(str(quantity)) * extracted_price
                            + (fee or Decimal("0"))
                            + (tax or Decimal("0"))
                        )
                if fee is not None:
                    change["fee"] = fee
                if tax is not None:
                    change["tax"] = tax

                required = {
                    "account": change.get("account"),
                    "code": change.get("code"),
                    "currency": change.get("currency"),
                    "quantity": change.get("quantity"),
                    "price": change.get("cost_price"),
                }
                absent = [key for key, value in required.items() if value in (None, "")]
                if absent:
                    raise ValueError(
                        f"第{index + 1}条缺少字段：{', '.join(absent)}"
                    )

                previous_asset = {
                    "code": str(change.get("code")),
                    "name": change.get("name"),
                    "currency": change.get("currency"),
                }
            else:
                if not change.get("account"):
                    raise ValueError(f"第{index + 1}条缺少账户")
                if child_action in {"deposit", "withdraw", "set_cash"}:
                    for field_name in ("currency", "amount"):
                        if change.get(field_name) in (None, ""):
                            raise ValueError(f"第{index + 1}条缺少字段：{field_name}")

            # Old parser may still report fields that V3 context filled.
            missing = [
                item for item in missing
                if not (
                    (item in {"account", "account/group"} and change.get("account"))
                    or (item == "code" and change.get("code"))
                    or (item == "currency" and change.get("currency"))
                    or (item == "quantity" and change.get("quantity") is not None)
                    or (item in {"cost_price", "cost_price_non_negative"} and change.get("cost_price") is not None)
                )
            ]
            if missing:
                raise ValueError(
                    f"第{index + 1}条仍缺少或存在无效字段：{', '.join(sorted(set(missing)))}"
                )

            event = transaction_from_change(
                change,
                effective_at=effective_at,
                source=source,
                date_precision=date_precision,
            )
            events.append(event)
            previous_account = event.account

        interpretations.append(TextClauseInterpretation(
            original=original,
            cleaned=cleaned,
            effective_at=effective_at,
            date_precision=date_precision,
            action_type=action,
            warnings=list(dict.fromkeys(warnings)),
        ))
        all_warnings.extend(warnings)

    return HistoricalTextParseResult(
        events=events,
        clauses=interpretations,
        warnings=list(dict.fromkeys(all_warnings)),
    )
