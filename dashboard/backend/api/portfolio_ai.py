"""
AI bookkeeping preview / confirm APIs.

Preview and revise never write portfolio.json. Only confirm writes, through
PortfolioWriteService with backup and operation logs.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import re
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.services.portfolio_write_service import (
    PENDING_DIR,
    PortfolioWriteService,
    ensure_data_dirs,
    strip_code_prefix,
    to_float,
    utc_now_iso,
)


router = APIRouter()

InputType = Literal["text", "image_text"]

COMMON_ACCOUNTS = {"长桥", "哈富", "IBKR", "尊嘉", "华盛通", "银河"}
ACCOUNT_ALIASES = {
    "IB": "IBKR",
    "盈透": "IBKR",
    "盈透证券": "IBKR",
}
COMMON_ASSET_WORDS = {"苹果", "微软", "英伟达", "特斯拉", "比亚迪", "腾讯", "腾讯控股", "小米", "海外科技", "纳指ETF"}
NEW_ACCOUNT_HINTS = {"富途", "老虎", "雪盈", "中信证券", "银河2号"}


class PreviewRequest(BaseModel):
    input_type: InputType = "text"
    message: str


class ReviseRequest(BaseModel):
    pending_id: str
    message: str


class ConfirmRequest(BaseModel):
    pending_id: str


class CancelRequest(BaseModel):
    pending_id: str


def pending_path(pending_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9-]{8,80}", pending_id):
        raise HTTPException(status_code=400, detail="非法 pending_id")
    return PENDING_DIR / f"{pending_id}.json"


def save_pending(pending: Dict[str, Any]) -> Dict[str, Any]:
    ensure_data_dirs()
    path = pending_path(pending["pending_id"])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pending, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return pending


def load_pending(pending_id: str) -> Dict[str, Any]:
    path = pending_path(pending_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="pending_action 不存在")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def public_pending(pending: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ok": True,
        "pending_id": pending.get("pending_id"),
        "summary": pending.get("summary"),
        "action_type": pending.get("action_type"),
        "changes": pending.get("changes", []),
        "missing_fields": pending.get("missing_fields", []),
        "warnings": pending.get("warnings", []),
        "requires_confirmation": pending.get("requires_confirmation", False),
        "intent": pending.get("intent", "bookkeeping"),
        "status": pending.get("status", "pending"),
        "operation_id": pending.get("operation_id"),
    }


def compact_positions() -> List[Dict[str, Any]]:
    service = PortfolioWriteService()
    portfolio = service.load_portfolio()
    return [
        {
            "account": p.get("account"),
            "name": p.get("name"),
            "code": p.get("code"),
            "currency": p.get("currency"),
            "asset_type": p.get("asset_type"),
            "quantity": p.get("quantity"),
            "cost_price": p.get("cost_price"),
        }
        for p in portfolio.get("positions", [])
    ]


def parse_key_value(message: str, key: str) -> Optional[str]:
    match = re.search(rf"{re.escape(key)}\s*[:：]\s*([^\s]+)", message, re.IGNORECASE)
    if not match:
        return None
    return match.group(1).strip()


def parse_first_key_value(message: str, keys: tuple[str, ...]) -> Optional[str]:
    for key in keys:
        value = parse_key_value(message, key)
        if value is not None:
            return value
    return None


def looks_like_structured_bookkeeping(message: str) -> bool:
    """Recognize complete key:value bookkeeping input even without an action verb."""
    label_groups = (
        ("account", "账户", "券商", "分组"),
        ("name", "名称", "标的"),
        ("code", "symbol", "代码"),
        ("currency", "币种"),
        ("asset_type", "type", "类型"),
        ("quantity", "数量"),
        ("cost_price", "成本价", "均价"),
        ("total_cost", "总成本", "总金额"),
    )
    present = {
        index
        for index, aliases in enumerate(label_groups)
        if any(re.search(rf"{re.escape(alias)}\s*[:：]", message, re.IGNORECASE) for alias in aliases)
    }
    has_identity = bool(present & {0, 1, 2})
    has_quantity = 5 in present
    has_cost = bool(present & {6, 7})
    return has_identity and has_quantity and has_cost and len(present) >= 4


def parse_number_after(message: str, labels: tuple[str, ...]) -> Optional[float]:
    label_pattern = "|".join(re.escape(label) for label in labels)
    match = re.search(rf"(?:{label_pattern})\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)", message, re.IGNORECASE)
    if match:
        return to_float(match.group(1))
    return None


def infer_currency(message: str) -> Optional[str]:
    lower = message.lower()
    if any(token in message for token in ["人民币", "港币", "美元"]):
        if "人民币" in message:
            return "CNY"
        if "港币" in message:
            return "HKD"
        if "美元" in message:
            return "USD"
    if "rmb" in lower or "cny" in lower or re.search(r"\d+\s*元", message):
        return "CNY"
    if "hkd" in lower:
        return "HKD"
    if "usd" in lower or "刀" in message:
        return "USD"
    return None


def normalize_account(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    account = str(value).strip()
    if not account:
        return None
    return ACCOUNT_ALIASES.get(account.upper(), ACCOUNT_ALIASES.get(account, account))


def infer_account(message: str, allow_single: bool = False) -> tuple[Optional[str], Optional[str]]:
    text = message.strip()
    if allow_single and re.fullmatch(r"[A-Za-z0-9\u4e00-\u9fff]{2,12}", text):
        return normalize_account(text), "single"

    account_match = re.search(r"(?:账户|券商|分组)(?:还是|为|是|:|：)?\s*([A-Za-z0-9\u4e00-\u9fff]{2,12})", text)
    if account_match:
        return normalize_account(account_match.group(1)), "explicit"

    same_match = re.search(r"(?:还是|同上账户|这个账户)\s*([A-Za-z0-9\u4e00-\u9fff]{2,12})", text)
    if same_match:
        return normalize_account(same_match.group(1)), "explicit"

    for account in sorted(COMMON_ACCOUNTS | NEW_ACCOUNT_HINTS, key=len, reverse=True):
        if re.search(rf"{re.escape(account)}(?:买了|买入|新增|卖了|卖出|入金|现金增加|转入)", text, re.IGNORECASE):
            return normalize_account(account), "explicit"

    prefix_match = re.match(r"^([A-Za-z0-9\u4e00-\u9fff]{2,12})(?:买了|买入|新增|卖了|卖出|入金|现金增加|转入)", text, re.IGNORECASE)
    if prefix_match:
        prefix = prefix_match.group(1)
        if prefix not in COMMON_ASSET_WORDS:
            return normalize_account(prefix), "candidate"
    return None, None


def infer_asset(message: str, existing_positions: List[Dict[str, Any]]) -> tuple[Dict[str, Any], List[str]]:
    warnings: List[str] = []
    name = parse_first_key_value(message, ("name", "名称", "标的"))
    raw_symbol = parse_first_key_value(message, ("symbol", "code", "代码"))
    currency = (parse_first_key_value(message, ("currency", "币种")) or infer_currency(message) or "").upper() or None
    asset_type = parse_first_key_value(message, ("asset_type", "type", "类型"))
    code = None

    if raw_symbol:
        code, inferred_currency, inferred_asset_type = strip_code_prefix(raw_symbol)
        currency = currency or inferred_currency
        asset_type = asset_type or inferred_asset_type

    if name:
        matched = match_existing(name, existing_positions)
        if matched:
            code = code or matched.get("code")
            currency = currency or matched.get("currency")
            asset_type = asset_type or matched.get("asset_type")

    if not name:
        message_lower = message.lower()
        for position in existing_positions:
            position_name = str(position.get("name") or "")
            position_code = str(position.get("code") or "")
            name_tokens = [
                token.strip().lower()
                for token in re.split(r"[/|\s]+", position_name)
                if token.strip()
            ]
            matched_existing = (
                bool(position_code and position_code.lower() in message_lower)
                or any(len(token) >= 2 and token in message_lower for token in name_tokens)
            )
            if matched_existing:
                name = position_name or position_code
                code = code or position_code
                currency = currency or position.get("currency")
                asset_type = asset_type or position.get("asset_type")
                break

    if "海外科技" in message and not code:
        name = "海外科技"
        asset_type = asset_type or "fund_or_custom"
        warnings.append("海外科技像是基金简称或自定义标的，需要确认具体代码或基金名称")
    elif "纳指ETF" in message and not code:
        name = "纳指ETF"
        asset_type = asset_type or ("fund_or_custom" if currency == "CNY" else "etf")
        warnings.append("纳指ETF存在多个可能标的，需要确认具体代码或基金名称")
    elif "苹果" in message:
        name = "Apple/苹果"
        code = code or "AAPL"
        currency = currency or "USD"
        asset_type = asset_type or "stock"
    elif "微软" in message:
        name = "Microsoft/微软"
        code = code or "MSFT"
        currency = currency or "USD"
        asset_type = asset_type or "stock"
    elif "英伟达" in message or "nvidia" in message.lower():
        name = "NVIDIA/英伟达"
        code = code or "NVDA"
        currency = currency or "USD"
        asset_type = asset_type or "stock"
    elif "特斯拉" in message:
        name = "Tesla/特斯拉"
        code = code or "TSLA"
        currency = currency or "USD"
        asset_type = asset_type or "stock"
    elif "比亚迪" in message:
        name = "比亚迪"
        if "港股" in message or "港币" in message or "HKG" in message.upper() or "比亚迪股份" in message:
            code = code or "1211"
            currency = currency or "HKD"
        else:
            code = code or "002594"
            currency = currency or "CNY"
            warnings.append("如实际是港股比亚迪股份，请修改")
        asset_type = asset_type or "stock"
    elif "腾讯" in message:
        name = "腾讯控股"
        code = code or "0700"
        currency = currency or "HKD"
        asset_type = asset_type or "stock"
    elif "小米" in message and ("港" in message or "HK" in message.upper()):
        name = "小米集团"
        code = code or "1810"
        currency = currency or "HKD"
        asset_type = asset_type or "stock"

    if not name and raw_symbol:
        name = code or raw_symbol

    if not currency and code:
        if re.fullmatch(r"\d{6}", code):
            currency = "CNY"
        elif re.fullmatch(r"\d{4}", code):
            currency = "HKD"
        elif re.fullmatch(r"[A-Z.]{1,8}", code):
            currency = "USD"

    if not asset_type and code:
        if re.fullmatch(r"\d{6}", code):
            asset_type = "fund" if code.startswith(("15", "16", "50", "51", "52", "56", "58")) else "stock"
        elif re.fullmatch(r"\d{4}", code) or re.fullmatch(r"[A-Z.]{1,8}", code):
            asset_type = "stock"

    if not currency and ("份" in message or "海外科技" in message) and re.search(r"\d+\s*元", message):
        currency = "CNY"
    if not asset_type and "份" in message:
        asset_type = "fund_or_custom"

    return {
        "name": name or "",
        "code": code,
        "currency": currency,
        "asset_type": asset_type,
    }, warnings


def match_existing(name: str, positions: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for position in positions:
        if name and (name == position.get("name") or name == position.get("code")):
            return position
    return None


def find_position_matches(asset: Dict[str, Any], positions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Find existing holdings for a parsed asset, preferring code+currency."""
    code = str(asset.get("code") or "").upper().strip()
    currency = str(asset.get("currency") or "").upper().strip()
    name = str(asset.get("name") or "").strip()

    matches: List[Dict[str, Any]] = []
    for position in positions:
        position_code = str(position.get("code") or "").upper().strip()
        position_currency = str(position.get("currency") or "").upper().strip()
        position_name = str(position.get("name") or "").strip()

        if code and position_code == code:
            if not currency or not position_currency or position_currency == currency:
                matches.append(position)
            continue

        if not code and name and position_name:
            if name == position_name or name in position_name or position_name in name:
                matches.append(position)

    return matches


def resolve_sell_account(
    account: Optional[str],
    asset: Dict[str, Any],
    positions: List[Dict[str, Any]],
    warnings: List[str],
) -> tuple[Optional[str], List[Dict[str, Any]]]:
    """Use the only existing holding account; otherwise require a user choice."""
    matches = find_position_matches(asset, positions)
    if account:
        selected = [
            position for position in matches
            if str(position.get("account") or "").strip() == account
        ]
        if not selected:
            warnings.append(f"账户{account}中未找到该持仓，暂不支持卖空")
        return account, selected

    accounts = sorted({
        str(position.get("account") or "").strip()
        for position in matches
        if str(position.get("account") or "").strip()
    })
    if len(accounts) == 1:
        warnings.append(f"根据现有持仓自动选择账户：{accounts[0]}")
        return accounts[0], matches
    if len(accounts) > 1:
        warnings.append(f"检测到多个账户持有该标的：{'、'.join(accounts)}，请选择卖出账户")
        return None, matches

    warnings.append("当前账户中未找到该持仓，暂不支持卖空")
    return None, []


def infer_action(message: str) -> str:
    if any(token in message for token in ["卖了", "卖出", "清仓", "减持"]) or re.search(r"卖(?:掉)?\s*[0-9]", message):
        return "sell"
    if any(token in message for token in ["入金", "现金增加", "转入"]):
        return "deposit"
    if any(token in message for token in ["买了", "买入", "新增", "添加", "持仓", "写入数据库", "帮我记上", "录入"]):
        return "add_or_update"
    if looks_like_structured_bookkeeping(message):
        return "add_or_update"
    return "chat_only"


def parse_quantity(message: str) -> Optional[float]:
    value = parse_number_after(message, ("quantity", "数量"))
    if value is not None:
        return value
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(?:股|份)", message)
    return to_float(match.group(1)) if match else None


def parse_amounts(message: str, quantity: Optional[float]) -> tuple[Optional[float], Optional[float], Optional[float]]:
    total_cost = parse_number_after(message, ("total_cost", "总成本", "总金额", "一共花了", "总共花了"))
    cost_price = parse_number_after(message, ("cost_price", "成本价", "均价", "成交价", "price"))
    fee = parse_number_after(message, ("fee", "手续费"))

    if total_cost is None:
        total_match = re.search(
            r"(?:(?:一共|总共|总计)\s*(?:花了|金额)?|(?:花了|总金额)\s*)"
            r"([0-9]+(?:\.[0-9]+)?)\s*(?:美元|港币|人民币|元|刀|USD|HKD|CNY)?",
            message,
            re.IGNORECASE,
        )
        if total_match:
            total_cost = to_float(total_match.group(1))
    if cost_price is None and total_cost is None:
        price_match = re.search(r"(?:均价|成本价|成交价)?\s*([0-9]+(?:\.[0-9]+)?)\s*(?:一股|每股|/股|美元|港币|元|刀)?\s*$", message, re.IGNORECASE)
        if price_match and "花了" not in message and "总" not in message and "一共" not in message:
            cost_price = to_float(price_match.group(1))
    if cost_price is None and quantity and total_cost is not None:
        cost_price = total_cost / quantity
    if total_cost is None and quantity is not None and cost_price is not None:
        total_cost = quantity * cost_price
    return cost_price, total_cost, fee


def build_missing(change: Dict[str, Any], action_type: str) -> List[str]:
    if action_type == "chat_only":
        return []
    if action_type == "deposit":
        missing = []
        if not change.get("account"):
            missing.append("account")
        if not change.get("currency"):
            missing.append("currency")
        if to_float(change.get("amount"), None) is None:
            missing.append("amount")
        return missing

    missing = []
    if not change.get("account"):
        missing.append("account")
    if not change.get("name") and not change.get("code"):
        missing.append("name/code")
    if not change.get("code"):
        missing.append("code")
    if not change.get("currency"):
        missing.append("currency")
    if not change.get("asset_type"):
        missing.append("asset_type")
    if to_float(change.get("quantity"), None) is None:
        missing.append("quantity")
    if to_float(change.get("cost_price"), None) is None and to_float(change.get("total_cost"), None) is None:
        missing.append("cost_price")
    return missing


def parse_bookkeeping_message(message: str, previous: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    service = PortfolioWriteService()
    existing_positions = compact_positions()
    text = message.strip()

    if previous and previous.get("changes") and previous.get("missing_fields") in (["account"], ["account/group"]):
        account, _ = infer_account(text, allow_single=True)
        if account:
            updated = previous["changes"][0].copy()
            updated["account"] = account
            updated["updated_at"] = utc_now_iso()
            warnings = [
                warning for warning in previous.get("warnings", [])
                if "新账户分组" not in warning
                and "检测到多个账户持有该标的" not in warning
                and "当前账户中未找到该持仓" not in warning
            ]
            action_type = previous.get("action_type", "add_or_update")
            if account not in COMMON_ACCOUNTS:
                warnings.append(f"{account} 是新账户分组，后续确认写入时可创建/使用")
            updated = service.normalize_position(updated)
            missing = build_missing(updated, action_type)
            if action_type == "sell":
                matches = find_position_matches(updated, existing_positions)
                selected = [
                    position for position in matches
                    if str(position.get("account") or "").strip() == account
                ]
                if not selected:
                    missing.append("existing_position")
                    warnings.append(f"账户{account}中未找到该持仓，暂不支持卖空")
                else:
                    available = sum(to_float(position.get("quantity"), 0.0) or 0.0 for position in selected)
                    sell_qty = to_float(updated.get("quantity"), 0.0) or 0.0
                    if sell_qty > available:
                        missing.append("available_quantity")
                        warnings.append(f"卖出数量{sell_qty:g}超过账户{account}现有持仓{available:g}，暂不支持卖空")
                    else:
                        warnings.append(f"已选择卖出账户：{account}")
                missing = list(dict.fromkeys(missing))
            return {
                "intent": "bookkeeping",
                "summary": "已补充账户分组",
                "action_type": previous.get("action_type", "add_or_update"),
                "changes": [updated],
                "missing_fields": missing,
                "warnings": warnings,
            }

    action_type = infer_action(text)
    if action_type == "chat_only":
        return {
            "intent": "chat_only",
            "summary": "普通聊天，不创建待确认记账信息",
            "action_type": "chat_only",
            "changes": [],
            "missing_fields": [],
            "warnings": [],
        }

    account, account_source = infer_account(text)
    asset, warnings = infer_asset(text, existing_positions)
    quantity = parse_quantity(text)
    cost_price, total_cost, fee = parse_amounts(text, quantity)

    sell_matches: List[Dict[str, Any]] = []
    if action_type == "sell":
        account, sell_matches = resolve_sell_account(
            account, asset, existing_positions, warnings
        )

    if account and account not in COMMON_ACCOUNTS and account_source in {"candidate", "single", "explicit"}:
        warnings.append(f"{account} 是新账户分组，后续确认写入时可创建/使用")

    change = {
        "action_type": action_type,
        "account": account,
        "name": asset.get("name"),
        "code": asset.get("code"),
        "currency": asset.get("currency"),
        "asset_type": asset.get("asset_type"),
        "quantity": quantity,
        "cost_price": cost_price,
        "total_cost": total_cost,
        "fee": fee,
        "note": "",
        "source": "ai",
    }
    change = {k: v for k, v in change.items() if v is not None}
    change = service.normalize_position(change)
    missing = build_missing(change, action_type)
    if action_type == "sell":
        if not sell_matches:
            missing.append("existing_position")
        elif account:
            selected = [
                position for position in sell_matches
                if str(position.get("account") or "").strip() == account
            ]
            if not selected:
                missing.append("existing_position")
            else:
                available = sum(to_float(position.get("quantity"), 0.0) or 0.0 for position in selected)
                if quantity is not None and quantity > available:
                    missing.append("available_quantity")
                    warnings.append(f"卖出数量{quantity:g}超过账户{account}现有持仓{available:g}，暂不支持卖空")
    if "market" in missing:
        missing.remove("market")
    missing = list(dict.fromkeys(missing))

    return {
        "intent": "bookkeeping",
        "summary": "识别到1条待确认记账信息",
        "action_type": action_type,
        "changes": [change],
        "missing_fields": missing,
        "warnings": sorted(set(warnings)),
    }


def make_pending(parsed: Dict[str, Any], input_type: str, message: str) -> Dict[str, Any]:
    pending_id = str(uuid4())
    now = datetime.now()
    missing = [field for field in parsed.get("missing_fields", []) if field != "market"]
    pending = {
        "ok": True,
        "pending_id": pending_id,
        "intent": parsed.get("intent", "bookkeeping"),
        "input_type": input_type,
        "message": message,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=30)).isoformat(),
        "status": "pending",
        "summary": parsed.get("summary", "识别到待确认记账信息"),
        "action_type": parsed.get("action_type", "add_or_update"),
        "changes": parsed.get("changes", []),
        "missing_fields": missing,
        "warnings": parsed.get("warnings", []),
        "requires_confirmation": parsed.get("intent") == "bookkeeping" and len(missing) == 0,
    }
    return pending


@router.post("/ai-preview")
async def ai_preview(request: PreviewRequest):
    parsed = parse_bookkeeping_message(request.message)
    if parsed.get("intent") == "chat_only":
        return {
            "ok": True,
            "intent": "chat_only",
            "summary": parsed["summary"],
            "requires_confirmation": False,
            "changes": [],
            "missing_fields": [],
            "warnings": [],
        }
    pending = make_pending(parsed, request.input_type, request.message)
    save_pending(pending)
    return public_pending(pending)


@router.post("/ai-revise")
async def ai_revise(request: ReviseRequest):
    pending = load_pending(request.pending_id)
    if pending.get("status") != "pending":
        raise HTTPException(status_code=400, detail="pending_action 已结束")
    parsed = parse_bookkeeping_message(request.message, previous=pending)
    if parsed.get("intent") == "chat_only":
        raise HTTPException(status_code=400, detail="未识别到可用于补充的记账信息")
    pending["message"] = f"{pending.get('message', '')}\n[revise] {request.message}"
    pending["changes"] = parsed.get("changes", pending.get("changes", []))
    pending["missing_fields"] = [field for field in parsed.get("missing_fields", []) if field != "market"]
    pending["warnings"] = parsed.get("warnings", [])
    pending["summary"] = parsed.get("summary", pending.get("summary"))
    pending["action_type"] = parsed.get("action_type", pending.get("action_type"))
    pending["requires_confirmation"] = len(pending["missing_fields"]) == 0
    pending["updated_at"] = utc_now_iso()
    save_pending(pending)
    return public_pending(pending)


@router.post("/ai-confirm")
async def ai_confirm(request: ConfirmRequest):
    pending = load_pending(request.pending_id)
    if pending.get("status") != "pending":
        raise HTTPException(status_code=400, detail="pending_action 已结束")
    expires_at = datetime.fromisoformat(pending["expires_at"])
    if expires_at < datetime.now():
        pending["status"] = "expired"
        save_pending(pending)
        raise HTTPException(status_code=400, detail="pending_action 已过期")
    if pending.get("missing_fields"):
        raise HTTPException(status_code=400, detail=f"仍有缺失字段: {', '.join(pending['missing_fields'])}")

    service = PortfolioWriteService()
    for change in pending.get("changes", []):
        missing = service.validate_confirmable_change(change)
        if missing:
            raise HTTPException(status_code=400, detail=f"无法确认，缺少: {', '.join(missing)}")

    try:
        result = service.safe_add_positions(
            pending.get("changes", []),
            summary=pending.get("summary", "AI确认写入"),
            pending_action=pending,
        )
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    pending["status"] = "confirmed"
    pending["confirmed_at"] = utc_now_iso()
    pending["operation_id"] = result["operation_id"]
    save_pending(pending)
    reload_agent_portfolio_provider()
    return result


@router.post("/ai-cancel")
async def ai_cancel(request: CancelRequest):
    pending = load_pending(request.pending_id)
    pending["status"] = "cancelled"
    pending["cancelled_at"] = utc_now_iso()
    save_pending(pending)
    return {"ok": True, "pending_id": request.pending_id, "status": "cancelled"}


@router.get("/pending/{pending_id}")
async def get_pending(pending_id: str):
    return public_pending(load_pending(pending_id))


@router.get("/operations")
async def list_operations():
    service = PortfolioWriteService()
    return {"operations": service.list_operations()}


@router.post("/rollback-latest")
async def rollback_latest():
    service = PortfolioWriteService()
    try:
        result = service.rollback_latest_operation()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    reload_agent_portfolio_provider()
    return result


@router.post("/rollback/{operation_id}")
async def rollback(operation_id: str):
    service = PortfolioWriteService()
    try:
        result = service.rollback_operation(operation_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="operation 不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    reload_agent_portfolio_provider()
    return result


def reload_agent_portfolio_provider() -> None:
    try:
        from backend.main import get_agent_service

        service = get_agent_service()
        if service is not None:
            service._init_portfolio_provider()
    except Exception as exc:
        print(f"[PortfolioAI] 持仓 provider 重载失败: {exc}")
