"""Holdings screenshot classification and reconciliation for the V3 ledger.

A holdings screenshot is evidence about account state at a point in time, not a
transaction. It is therefore stored separately from the append-only transaction
ledger and only used to compare broker-reported state against replayed state.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .models import SUPPORTED_CURRENCIES, Transaction
from .replay import ReplayError, replay_transactions
from .screenshot import (
    _extract_json,
    _normalize_security_code,
    _ocr_tile_sync,
    create_configured_text_provider,
    prepare_image_tiles,
)


HOLDINGS_PROMPT = """你收到一张券商“当前持仓/资产”页面的 OCR 文本。
这不是成交历史。只提取截图中明确存在的快照事实，不要伪造成买入或卖出交易。

输出严格 JSON：
{
  "account": "券商/账户名或null",
  "positions": [
    {
      "code": "证券代码或null",
      "name": "证券名称或null",
      "quantity": "decimal或null",
      "average_cost": "decimal或null",
      "current_price": "decimal或null",
      "market_value": "decimal或null",
      "currency": "CNY|USD|HKD|null",
      "unrealized_pnl": "decimal或null",
      "confidence": 0.0,
      "inferred_fields": [],
      "warnings": []
    }
  ],
  "cash": [
    {
      "currency": "CNY|USD|HKD|null",
      "amount": "decimal或null",
      "label": "截图原标签",
      "is_cash": true,
      "confidence": 0.0
    }
  ],
  "summary": {},
  "warnings": []
}

规则：
1. “持仓数量/数量”必须是截图直接显示或能明确对应的数量。若只能通过市值/现价推算，quantity填null，并在warnings说明。
2. average_cost允许负值，因为券商调整后成本可能为负；不要把它解释为买入价格。
3. current_price只是截图时行情，不是交易事实。
4. “购买力/SMA/剩余流动性/可用国债/保证金”等不能当现金，is_cash=false。
5. 明确的“现金/现金余额/USD Cash/HKD Cash/CNY Cash”才is_cash=true。
6. 没有代码时不要凭空造代码；常见且唯一明确的名称可给出候选，但必须在inferred_fields标记code。
7. 只输出JSON。
"""


def _decimal(value: Any) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid decimal: {value}") from exc
    if not number.is_finite():
        raise ValueError(f"invalid decimal: {value}")
    return number


def _decimal_text(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else format(value, "f")


def _filename_captured_at(filename: str) -> str:
    match = re.search(
        r"Screenshot[_-](\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})",
        str(filename or ""),
        re.IGNORECASE,
    )
    if not match:
        return datetime.now().astimezone().isoformat()
    year, month, day, hour, minute, second = map(int, match.groups())
    # These screenshots are produced on the user's phone in China/HK time in
    # the current workflow. The explicit offset prevents later UTC ambiguity.
    return datetime(
        year, month, day, hour, minute, second,
        tzinfo=timezone(timedelta(hours=8)),
    ).isoformat()


def _detected_account(text: str, filename: str) -> Optional[str]:
    normalized = str(text or "")
    lowered_name = str(filename or "").lower()
    if "中国银河证券" in normalized or "银河证券" in normalized:
        return "银河"
    if "长桥" in normalized or "longbridge" in lowered_name:
        return "长桥"
    if "IBKR" in normalized.upper() or "INTERACTIVE BROKERS" in normalized.upper():
        return "IBKR"
    return None


def classify_ocr_text(text: str) -> str:
    text = str(text or "")
    trade_score = 0
    holdings_score = 0

    for token, weight in (
        ("历史订单", 6),
        ("订单查询", 5),
        ("方向/状态", 4),
        ("已成交", 2),
        ("已撤单", 2),
        ("已过期", 2),
        ("委托", 1),
    ):
        if token in text:
            trade_score += weight

    for token, weight in (
        ("当前持仓", 6),
        ("持仓明细", 6),
        ("投资组合", 5),
        ("总资产", 4),
        ("总市值", 4),
        ("证券市值", 4),
        ("市场价值", 3),
        ("持仓盈亏", 3),
        ("平均价格", 2),
        ("现金余额", 3),
        ("总计现金", 3),
        ("持仓", 2),
        ("余额", 1),
        ("成本", 1),
        ("市值", 1),
        ("现金", 1),
    ):
        if token in text:
            holdings_score += weight

    if trade_score >= 5 and trade_score >= holdings_score:
        return "trade_history"
    if holdings_score >= 4:
        return "holdings_snapshot"
    return "unknown"


async def ocr_image(
    filename: str,
    content: bytes,
) -> Tuple[str, Tuple[int, int], List[Dict[str, Any]], str]:
    image_hash, dimensions, tiles = prepare_image_tiles(content, filename)
    rows: List[Dict[str, Any]] = []

    for tile in tiles:
        tile_rows = await asyncio.to_thread(_ocr_tile_sync, tile)
        for row in tile_rows:
            confidence = float(row.get("confidence") or 0)
            if confidence < 0.30:
                continue
            rows.append(
                {
                    "text": str(row.get("text") or "").strip(),
                    "confidence": round(confidence, 4),
                    "x": round(float(row.get("x") or 0), 1),
                    "y": round(float(tile.y0 + float(row.get("y") or 0)), 1),
                    "tile": tile.index,
                }
            )

    rows.sort(key=lambda item: (item["y"], item["x"]))
    deduped: List[Dict[str, Any]] = []
    for row in rows:
        duplicate = any(
            existing["text"] == row["text"]
            and abs(float(existing["y"]) - float(row["y"])) <= 8
            for existing in deduped[-20:]
        )
        if not duplicate:
            deduped.append(row)

    text = "\n".join(row["text"] for row in deduped if row["text"])
    return image_hash, dimensions, deduped, text


async def classify_screenshot(
    filename: str,
    content: bytes,
) -> Dict[str, Any]:
    image_hash, dimensions, rows, text = await ocr_image(filename, content)
    return {
        "kind": classify_ocr_text(text),
        "filename": filename,
        "sha256": image_hash,
        "width": dimensions[0],
        "height": dimensions[1],
        "ocr_rows": rows,
        "ocr_text": text,
    }


def _normalize_position(row: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    warnings: List[str] = [str(item) for item in (row.get("warnings") or [])]
    currency = str(row.get("currency") or "").strip().upper() or None
    if currency and currency not in SUPPORTED_CURRENCIES:
        warnings.append(f"不支持的币种：{currency}")
        currency = None

    raw_code = str(row.get("code") or "").strip().upper() or None
    raw_text = " ".join(
        str(item)
        for item in (row.get("name"), row.get("code"))
        if item
    )
    code = _normalize_security_code(raw_code, currency, raw_text)
    name = str(row.get("name") or "").strip() or None
    quantity = _decimal(row.get("quantity"))
    average_cost = _decimal(row.get("average_cost"))
    current_price = _decimal(row.get("current_price"))
    market_value = _decimal(row.get("market_value"))
    unrealized_pnl = _decimal(row.get("unrealized_pnl"))

    if quantity is not None and quantity <= 0:
        warnings.append("持仓数量必须大于0")
        quantity = None
    if current_price is not None and current_price < 0:
        warnings.append("现价不能为负")
        current_price = None
    if market_value is not None and market_value < 0:
        warnings.append("市值不能为负")
        market_value = None

    inferred_fields = [
        str(item)
        for item in (row.get("inferred_fields") or [])
        if str(item).strip()
    ]

    # Arithmetic is used only as a consistency check, never to silently fill a
    # missing broker-reported quantity. A quantity that contradicts broker P&L
    # is safer to demote to unknown than to preserve a likely OCR digit error.
    if (
        quantity is not None
        and average_cost is not None
        and current_price is not None
        and unrealized_pnl is not None
    ):
        expected_pnl = (current_price - average_cost) * quantity
        pnl_tolerance = max(
            Decimal("0.10"),
            abs(unrealized_pnl) * Decimal("0.03"),
        )
        if abs(expected_pnl - unrealized_pnl) > pnl_tolerance:
            implied_quantity = None
            unit_pnl = current_price - average_cost
            if unit_pnl != 0:
                implied_quantity = unrealized_pnl / unit_pnl
            warnings.append(
                "截图数量与(现价-成本)×数量计算出的未实现盈亏不一致；"
                + (
                    f"按盈亏反推数量约为{implied_quantity.quantize(Decimal('0.01'))}，"
                    if implied_quantity is not None
                    else ""
                )
                + "已将数量降级为未知，需人工确认"
            )
            quantity = None

    if (
        quantity is not None
        and current_price not in (None, Decimal("0"))
        and market_value is not None
    ):
        expected_value = quantity * current_price
        tolerance = max(Decimal("0.05"), abs(market_value) * Decimal("0.003"))
        if abs(expected_value - market_value) > tolerance:
            warnings.append(
                f"数量×现价={expected_value} 与截图市值={market_value} 不一致，请人工核对"
            )

    if not name and not code:
        return None, warnings + ["持仓缺少名称和代码"]

    return {
        "code": code,
        "reported_code": raw_code,
        "name": name,
        "quantity": _decimal_text(quantity),
        "average_cost": _decimal_text(average_cost),
        "current_price": _decimal_text(current_price),
        "market_value": _decimal_text(market_value),
        "currency": currency,
        "unrealized_pnl": _decimal_text(unrealized_pnl),
        "confidence": row.get("confidence"),
        "inferred_fields": inferred_fields,
        "warnings": list(dict.fromkeys(warnings)),
    }, warnings


def _normalize_cash(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not bool(row.get("is_cash")):
        return None
    currency = str(row.get("currency") or "").strip().upper() or None
    if currency not in SUPPORTED_CURRENCIES:
        return None
    amount = _decimal(row.get("amount"))
    if amount is None:
        return None
    return {
        "currency": currency,
        "amount": _decimal_text(amount),
        "label": str(row.get("label") or ""),
        "confidence": row.get("confidence"),
    }


async def extract_holdings_snapshot(
    classification: Dict[str, Any],
    *,
    account_hint: str = "",
    captured_at_hint: str = "",
) -> Dict[str, Any]:
    if classification.get("kind") != "holdings_snapshot":
        raise ValueError("image is not classified as holdings_snapshot")

    provider, model_label = create_configured_text_provider()
    response = await provider.chat(
        [
            {
                "role": "system",
                "content": "你是保守的券商持仓快照解析器。只输出JSON，不创建交易。",
            },
            {
                "role": "user",
                "content": HOLDINGS_PROMPT
                + "\n\nOCR lines:\n"
                + json.dumps(classification.get("ocr_rows") or [], ensure_ascii=False),
            },
        ],
        temperature=0,
        max_tokens=5000,
    )
    parsed = _extract_json(response.content)

    account = str(account_hint or parsed.get("account") or "").strip()
    if not account:
        account = _detected_account(
            str(classification.get("ocr_text") or ""),
            str(classification.get("filename") or ""),
        ) or ""

    warnings = [str(item) for item in (parsed.get("warnings") or [])]
    if not account:
        warnings.append("无法从截图确定账户；保存对账快照前需要补充账户")

    positions: List[Dict[str, Any]] = []
    for row in parsed.get("positions") or []:
        if not isinstance(row, dict):
            continue
        position, row_warnings = _normalize_position(row)
        warnings.extend(row_warnings)
        if position is not None:
            positions.append(position)

    cash: List[Dict[str, Any]] = []
    for row in parsed.get("cash") or []:
        if not isinstance(row, dict):
            continue
        item = _normalize_cash(row)
        if item is not None:
            cash.append(item)

    captured_at = str(captured_at_hint or "").strip() or _filename_captured_at(
        str(classification.get("filename") or "")
    )

    return {
        "captured_at": captured_at,
        "account": account,
        "source": "screenshot_reconciliation",
        "source_hash": str(classification.get("sha256") or ""),
        "filename": str(classification.get("filename") or ""),
        "model_label": model_label,
        "positions": positions,
        "cash": cash,
        "metadata": {
            "image_width": classification.get("width"),
            "image_height": classification.get("height"),
            "summary": parsed.get("summary") or {},
            "ocr_row_count": len(classification.get("ocr_rows") or []),
            "warnings": list(dict.fromkeys(warnings)),
        },
        "warnings": list(dict.fromkeys(warnings)),
    }


def _key(account: str, code: str, currency: str) -> Tuple[str, str, str]:
    return (str(account), str(code).upper(), str(currency).upper())


def compare_snapshot_to_ledger(
    snapshot: Dict[str, Any],
    transactions: Sequence[Transaction],
) -> Dict[str, Any]:
    account = str(snapshot.get("account") or "").strip()
    captured_at = str(snapshot.get("captured_at") or "").strip()
    if not account:
        return {
            "status": "needs_account",
            "summary": {"matches": 0, "differences": 0},
            "positions": [],
            "cash": [],
            "replay_error": None,
        }

    try:
        state = replay_transactions(transactions, as_of=captured_at)
    except (ReplayError, ValueError) as exc:
        return {
            "status": "replay_error",
            "summary": {"matches": 0, "differences": 0},
            "positions": [],
            "cash": [],
            "replay_error": str(exc),
        }

    broker_positions: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    unresolved_identity_rows: List[Dict[str, Any]] = []
    for position in snapshot.get("positions") or []:
        code = str(position.get("code") or "").strip().upper()
        currency = str(position.get("currency") or "").strip().upper()
        if not code or not currency:
            unresolved_identity_rows.append(position)
            continue
        broker_positions[_key(account, code, currency)] = position

    ledger_positions = {
        key: value
        for key, value in state.positions.items()
        if key[0] == account
    }

    rows: List[Dict[str, Any]] = []
    matches = 0
    differences = 0

    for unresolved_position in unresolved_identity_rows:
        differences += 1
        rows.append(
            {
                "account": account,
                "code": str(unresolved_position.get("code") or ""),
                "currency": str(unresolved_position.get("currency") or ""),
                "name": str(unresolved_position.get("name") or ""),
                "status": "snapshot_identity_unresolved",
                "snapshot_quantity": unresolved_position.get("quantity"),
                "ledger_quantity": None,
                "quantity_difference": None,
                "snapshot_average_cost": unresolved_position.get("average_cost"),
                "ledger_average_cost": None,
                "cost_difference": None,
            }
        )

    for position_key in sorted(set(broker_positions) | set(ledger_positions)):
        broker = broker_positions.get(position_key)
        ledger = ledger_positions.get(position_key)
        broker_qty = _decimal(broker.get("quantity")) if broker else None
        ledger_qty = ledger.quantity if ledger else None

        if broker is None:
            status = "missing_in_snapshot"
        elif ledger is None:
            status = "missing_in_ledger"
        elif broker_qty is None:
            status = "snapshot_quantity_unknown"
        elif broker_qty == ledger_qty:
            status = "match"
        else:
            status = "quantity_mismatch"

        if status == "match":
            matches += 1
        else:
            differences += 1

        broker_cost = _decimal(broker.get("average_cost")) if broker else None
        ledger_cost = ledger.average_cost if ledger else None
        rows.append(
            {
                "account": account,
                "code": position_key[1],
                "currency": position_key[2],
                "name": (broker or {}).get("name") or (ledger.name if ledger else ""),
                "status": status,
                "snapshot_quantity": _decimal_text(broker_qty),
                "ledger_quantity": _decimal_text(ledger_qty),
                "quantity_difference": _decimal_text(
                    broker_qty - ledger_qty
                    if broker_qty is not None and ledger_qty is not None
                    else None
                ),
                "snapshot_average_cost": _decimal_text(broker_cost),
                "ledger_average_cost": _decimal_text(ledger_cost),
                "cost_difference": _decimal_text(
                    broker_cost - ledger_cost
                    if broker_cost is not None and ledger_cost is not None
                    else None
                ),
            }
        )

    broker_cash = {
        (account, str(item.get("currency") or "").upper()): _decimal(item.get("amount"))
        for item in snapshot.get("cash") or []
        if item.get("currency")
    }
    ledger_cash = {
        key: value
        for key, value in state.cash.items()
        if key[0] == account
    }

    cash_rows: List[Dict[str, Any]] = []
    for cash_key in sorted(set(broker_cash) | set(ledger_cash)):
        broker_amount = broker_cash.get(cash_key)
        ledger_amount = ledger_cash.get(cash_key)
        if broker_amount is None:
            status = "missing_in_snapshot"
        elif ledger_amount is None:
            status = "missing_in_ledger"
        elif broker_amount == ledger_amount:
            status = "match"
        else:
            status = "cash_mismatch"
        if status == "match":
            matches += 1
        else:
            differences += 1
        cash_rows.append(
            {
                "account": account,
                "currency": cash_key[1],
                "status": status,
                "snapshot_amount": _decimal_text(broker_amount),
                "ledger_amount": _decimal_text(ledger_amount),
                "difference": _decimal_text(
                    broker_amount - ledger_amount
                    if broker_amount is not None and ledger_amount is not None
                    else None
                ),
            }
        )

    return {
        "status": "match" if differences == 0 else "differences",
        "summary": {"matches": matches, "differences": differences},
        "positions": rows,
        "cash": cash_rows,
        "replay_error": None,
    }
