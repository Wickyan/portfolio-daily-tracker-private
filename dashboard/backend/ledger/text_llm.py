"""LLM-assisted natural-language parser for the V3 transaction ledger.

The LLM only interprets language. Every returned row is normalized and
validated into the immutable V3 Transaction model before it can become a
pending batch. If this layer fails, callers can fall back to the deterministic
legacy/rule parser in text_adapter.py.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from .models import Transaction, TransactionType
from .text_adapter import (
    HistoricalTextParseResult,
    TextClauseInterpretation,
)
from .screenshot import (
    _extract_json,
    _normalize_security_code,
    create_configured_text_provider,
)


TEXT_LEDGER_PROMPT = """你把用户的投资记账自然语言转换成严格 JSON。你只负责理解，不负责写数据库。

当前参考时间：{reference}

规则：
1. account 允许任意用户账户名，例如“长桥”“IBKR”“银河”“账户③”“尊嘉2号”，不要因为不是已知枚举而拒绝。
2. 支持 BUY/SELL/DEPOSIT/WITHDRAW/DIVIDEND/FX/OPENING_POSITION/OPENING_CASH。
3. 对 BUY/SELL：quantity 是成交数量，price 是每股/每份成交单价。用户明确说“总共/一共花了”才放 total_amount。
4. fee/tax 未提供必须为 null，不得编造 0 是券商原始值。
5. effective_at 用 ISO 日期或 ISO datetime。相对日期按当前参考时间解释。
6. 用户给出“账户③”就原样保留 account="账户③"。
7. 常见标的可解析：小米/小米集团-W -> 01810.HK/HKD；苹果 -> AAPL/USD；英伟达 -> NVDA/USD；微软 -> MSFT/USD；特斯拉 -> TSLA/USD；比亚迪无港股/ADR语境时优先 002594/CNY。
8. 不确定的证券代码不要乱猜，填 null；但已经明确给出的代码必须保留。
9. 不要做投资建议，不要根据现有持仓阻止历史卖出。
10. 一句话含多笔交易时拆成多行。
11. 只输出 JSON，不要 markdown。

格式：
{
  "rows": [
    {
      "event_type": "BUY|SELL|DEPOSIT|WITHDRAW|DIVIDEND|FX|OPENING_POSITION|OPENING_CASH",
      "effective_at": "YYYY-MM-DD or ISO datetime",
      "account": "string or null",
      "code": "string or null",
      "name": "string or null",
      "currency": "CNY|USD|HKD|null",
      "quantity": "decimal or null",
      "price": "decimal or null",
      "total_amount": "decimal or null",
      "amount": "decimal or null",
      "fee": "decimal or null",
      "tax": "decimal or null",
      "counter_currency": "CNY|USD|HKD|null",
      "counter_amount": "decimal or null",
      "confidence": 0.0,
      "inferred_fields": ["code", "currency"],
      "raw_text": "支持本行的原始短句"
    }
  ],
  "warnings": []
}
"""


_NAME_ALIASES = {
    "小米": ("01810.HK", "HKD", "小米集团-W"),
    "小米集团": ("01810.HK", "HKD", "小米集团-W"),
    "小米集团-w": ("01810.HK", "HKD", "小米集团-W"),
    "苹果": ("AAPL", "USD", "Apple"),
    "apple": ("AAPL", "USD", "Apple"),
    "英伟达": ("NVDA", "USD", "NVIDIA"),
    "nvidia": ("NVDA", "USD", "NVIDIA"),
    "微软": ("MSFT", "USD", "Microsoft"),
    "microsoft": ("MSFT", "USD", "Microsoft"),
    "特斯拉": ("TSLA", "USD", "Tesla"),
    "tesla": ("TSLA", "USD", "Tesla"),
}


def _decimal(value: Any) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"无效数字：{value}") from exc
    if not result.is_finite():
        raise ValueError(f"无效数字：{value}")
    return result


def _event_type(value: Any) -> TransactionType:
    token = str(value or "").strip().upper()
    aliases = {
        "BUY": TransactionType.BUY,
        "SELL": TransactionType.SELL,
        "DEPOSIT": TransactionType.DEPOSIT,
        "WITHDRAW": TransactionType.WITHDRAW,
        "DIVIDEND": TransactionType.DIVIDEND,
        "FX": TransactionType.FX,
        "OPENING_POSITION": TransactionType.OPENING_POSITION,
        "OPENING_CASH": TransactionType.OPENING_CASH,
    }
    if token not in aliases:
        raise ValueError(f"大模型返回不支持的记账类型：{token or 'empty'}")
    return aliases[token]


def _known_alias(
    name: Optional[str],
    code: Optional[str],
    currency: Optional[str],
) -> tuple[Optional[str], Optional[str], Optional[str], List[str]]:
    inferred: List[str] = []
    if code:
        return code, currency, name, inferred
    normalized_name = str(name or "").strip().lower()
    alias = _NAME_ALIASES.get(normalized_name)
    if not alias:
        return code, currency, name, inferred
    alias_code, alias_currency, alias_name = alias
    inferred.append("code")
    if not currency:
        currency = alias_currency
        inferred.append("currency")
    return alias_code, currency, name or alias_name, inferred


def _row_to_transaction(
    row: Dict[str, Any],
    *,
    source: str,
    model_label: str,
    original_message: str,
) -> tuple[Transaction, List[str]]:
    event_type = _event_type(row.get("event_type"))
    account = str(row.get("account") or "").strip()
    if not account:
        raise ValueError("大模型解析结果缺少账户")

    effective_at = str(row.get("effective_at") or "").strip()
    if not effective_at:
        raise ValueError("大模型解析结果缺少交易日期/时间")

    currency = str(row.get("currency") or "").strip().upper() or None
    raw_text = str(row.get("raw_text") or original_message).strip()
    reported_code = str(row.get("code") or "").strip().upper() or None
    name = str(row.get("name") or "").strip() or None
    reported_code, currency, name, alias_inferred = _known_alias(
        name,
        reported_code,
        currency,
    )
    code = _normalize_security_code(reported_code, currency, raw_text)

    quantity = _decimal(row.get("quantity"))
    price = _decimal(row.get("price"))
    total_amount = _decimal(row.get("total_amount"))
    amount = _decimal(row.get("amount"))
    fee_value = _decimal(row.get("fee"))
    tax_value = _decimal(row.get("tax"))
    fee = fee_value or Decimal("0")
    tax = tax_value or Decimal("0")
    warnings: List[str] = []

    if event_type in {TransactionType.BUY, TransactionType.SELL, TransactionType.OPENING_POSITION}:
        if price is None and total_amount is not None and quantity not in (None, Decimal("0")):
            price = total_amount / quantity
            warnings.append("文字只给出总成交金额，已按数量折算单价；请核对")
        if not code:
            raise ValueError("大模型解析结果缺少证券代码")
        if not currency:
            raise ValueError("大模型解析结果缺少币种")
        if quantity is None:
            raise ValueError("大模型解析结果缺少数量")
        if price is None:
            raise ValueError("大模型解析结果缺少成交单价")
        if fee_value is None:
            warnings.append("文字未提供手续费，暂按0计")
        if tax_value is None:
            warnings.append("文字未提供税费，暂按0计")

    if event_type in {
        TransactionType.DEPOSIT,
        TransactionType.WITHDRAW,
        TransactionType.DIVIDEND,
        TransactionType.OPENING_CASH,
    }:
        if amount is None:
            amount = total_amount
        if not currency:
            raise ValueError("大模型解析结果缺少币种")
        if amount is None:
            raise ValueError("大模型解析结果缺少金额")

    counter_currency = str(row.get("counter_currency") or "").strip().upper() or None
    counter_amount = _decimal(row.get("counter_amount"))

    inferred_fields = [
        str(item)
        for item in (row.get("inferred_fields") or [])
        if str(item).strip()
    ]
    inferred_fields.extend(alias_inferred)

    transaction = Transaction(
        event_type=event_type,
        effective_at=effective_at,
        account=account,
        code=code,
        name=name,
        currency=currency,
        quantity=quantity,
        price=price,
        fee=fee,
        tax=tax,
        amount=amount,
        counter_currency=counter_currency,
        counter_amount=counter_amount,
        source=source,
        metadata={
            "input": "text",
            "parser": "llm",
            "model": model_label,
            "confidence": row.get("confidence"),
            "raw_text": raw_text,
            "inferred_fields": sorted(set(inferred_fields)),
            "fee_observed": fee_value is not None,
            "tax_observed": tax_value is not None,
        },
    ).validated()
    return transaction, warnings


async def parse_historical_bookkeeping_text_with_llm(
    message: str,
    *,
    reference: Optional[datetime] = None,
    source: str = "text",
) -> HistoricalTextParseResult:
    raw_message = str(message or "").strip()
    if not raw_message:
        raise ValueError("empty bookkeeping message")

    provider, model_label = create_configured_text_provider()
    reference_text = (
        reference.isoformat()
        if reference is not None
        else datetime.now().astimezone().isoformat()
    )
    prompt = TEXT_LEDGER_PROMPT.replace("{reference}", reference_text)

    response = await provider.chat(
        [
            {
                "role": "system",
                "content": "你是保守的证券交易记账解析器。只输出严格 JSON。",
            },
            {
                "role": "user",
                "content": prompt + "\n\n用户输入：\n" + raw_message,
            },
        ],
        temperature=0,
        max_tokens=5000,
    )
    parsed = _extract_json(response.content)
    rows = parsed.get("rows") or parsed.get("transactions") or []
    if not isinstance(rows, list) or not rows:
        raise ValueError("大模型没有返回可记录的交易")

    events: List[Transaction] = []
    clauses: List[TextClauseInterpretation] = []
    warnings = [str(item) for item in (parsed.get("warnings") or [])]

    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"大模型第{index + 1}条结果不是对象")
        event, event_warnings = _row_to_transaction(
            row,
            source=source,
            model_label=model_label,
            original_message=raw_message,
        )
        events.append(event)
        warnings.extend(event_warnings)
        clauses.append(
            TextClauseInterpretation(
                original=str(row.get("raw_text") or raw_message),
                cleaned=str(row.get("raw_text") or raw_message),
                effective_at=str(event.effective_at),
                date_precision="llm",
                action_type=event.event_type.value,
                warnings=list(event_warnings),
            )
        )

    warnings.append(f"文字由 {model_label} 解释，写入前仍经过V3确定性校验")
    return HistoricalTextParseResult(
        events=events,
        clauses=clauses,
        warnings=list(dict.fromkeys(warnings)),
    )
