"""
AI bookkeeping preview / confirm APIs.

Preview and revise never write portfolio.json. Only confirm writes, through
PortfolioWriteService with backup and operation logs.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Literal, Optional
import unicodedata
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.services.instrument_search_service import InstrumentSearchService
from backend.services.portfolio_write_service import (
    PENDING_DIR,
    PORTFOLIO_MUTATION_LOCK,
    SUPPORTED_CURRENCIES,
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

NUMBER_TOKEN_PATTERN = r"[+-]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?\s*(?:[kKwW]|千|万)?"
CURRENCY_TOKEN_PATTERN = r"(?:HK\$|US\$|人民币元|人民币|港币|港元|美元|美金|美刀|欧元|日元|英镑|新加坡元|新币|澳元|加元|瑞郎|CNY|RMB|HKD|USD|EUR|JPY|GBP|SGD|AUD|CAD|CHF|[¥￥$]|元|刀)"
CURRENCY_ALIASES = {
    "人民币": "CNY",
    "元": "CNY",
    "CNY": "CNY",
    "RMB": "CNY",
    "港币": "HKD",
    "港元": "HKD",
    "HKD": "HKD",
    "美元": "USD",
    "美金": "USD",
    "美刀": "USD",
    "USD": "USD",
    "刀": "USD",
    "欧元": "EUR",
    "EUR": "EUR",
    "日元": "JPY",
    "JPY": "JPY",
    "英镑": "GBP",
    "GBP": "GBP",
    "新加坡元": "SGD",
    "新币": "SGD",
    "SGD": "SGD",
    "澳元": "AUD",
    "AUD": "AUD",
    "加元": "CAD",
    "CAD": "CAD",
    "瑞郎": "CHF",
    "CHF": "CHF",
    "¥": "CNY",
    "￥": "CNY",
    "$": "USD",
    "US$": "USD",
    "HK$": "HKD",
}


def parse_human_number(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    text = unicodedata.normalize("NFKC", str(value)).strip()
    match = re.fullmatch(
        r"([+-]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?)\s*([kKwW]|千|万)?",
        text,
    )
    if not match:
        return default
    number = float(match.group(1).replace(",", ""))
    if not math.isfinite(number):
        return default
    suffix = (match.group(2) or "").lower()
    multiplier = 1.0
    if suffix in {"k", "千"}:
        multiplier = 1000.0
    elif suffix in {"w", "万"}:
        multiplier = 10000.0
    result = number * multiplier
    return result if math.isfinite(result) else default


def format_human_number(value: float) -> str:
    number = float(value)
    if number.is_integer():
        return f"{number:.0f}"
    return f"{number:.12f}".rstrip("0").rstrip(".")


def malformed_number_reason(message: str) -> Optional[str]:
    """Detect numeric text that would otherwise be partially parsed."""
    text = str(message or "")
    if re.search(r"(?<![A-Za-z0-9])[+-]?(?:\d+(?:\.\d+)?)[eE][+-]?\d+", text):
        return "暂不支持科学计数法，请输入完整数字"
    if re.search(r"\d+\.\d+\.\d+", text):
        return "数字中包含多个小数点"
    if re.search(r"\d+(?:\.\d+)?\s*(?:万|千|[kKwW])\s*\d", text):
        return "暂不支持“1万2千”这类复合金额，请换算成完整数字"
    for token in re.findall(r"\d+(?:,\d+)+", text):
        if not re.fullmatch(r"\d{1,3}(?:,\d{3})+", token):
            return "千分位逗号格式不正确"
    return None


def non_actionable_statement(message: str) -> bool:
    """Avoid creating bookkeeping cards from negations, plans or how-to questions."""
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(message or "")))
    if not compact:
        return False
    if re.search(r"(?:不要|别|没有|并未|未曾|还没|尚未|不是|取消)", compact):
        return True
    if re.match(r"^(?:请问|怎么|如何|如果|假如|是否|要不要|能不能|能否|能给|可不可以|为什么|我想知道|我想|你觉得|可能|也许|帮我看看|查询)", compact):
        return True
    if re.match(r"^(?:我)?(?:计划|打算|准备|考虑|预计|明天|后天|下周|下个月|以后)", compact):
        return True
    if re.search(r"(?:会怎样|会怎么样|怎么办|怎么操作|如何操作|多少人民币|折合多少|等于多少|手续费是多少|合适吗|可以吗|行吗|好吗|是多少|吗)[？?]?$", compact):
        return True
    return False


def split_user_clauses(message: str) -> List[str]:
    broker = r"(?:长桥|哈富|IBKR|IB|盈透证券|盈透|尊嘉|华盛通|银河|富途|老虎|雪盈|中信证券)"
    action = r"(?:入金|出金|提现|取出|增加|减少|转入|转出|存入|充值|买入|卖出|清仓)"
    splitter = (
        rf"[；;\n]+"
        rf"|[，,](?=\s*(?:(?:再|然后|接着)\s*)?(?:(?:给|往|向|从|在)\s*)?{broker})"
        rf"|[，,](?=\s*(?:再|然后|接着)\s*{action})"
        rf"|(?:然后|接着|再)(?=\s*(?:(?:给|往|向|从|在)\s*)?(?:{broker}\s*)?{action})"
        rf"|和(?=\s*(?:(?:给|往|向|从|在)\s*)?{broker}\s*{action})"
        rf"|(?<=[。！？!?])"
    )
    return [
        re.sub(r"^(?:再|然后|接着)\s*", "", part.strip())
        for part in re.split(splitter, unicodedata.normalize("NFKC", str(message or "")), flags=re.IGNORECASE)
        if part.strip()
    ]


def normalize_currency_token(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    token = str(value).strip()
    return CURRENCY_ALIASES.get(token.upper(), CURRENCY_ALIASES.get(token))


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
    tmp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(pending, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)
    return pending


def load_pending(pending_id: str) -> Dict[str, Any]:
    path = pending_path(pending_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="pending_action不存在")
    try:
        with open(path, "r", encoding="utf-8") as f:
            pending = json.load(f)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="pending_action文件损坏") from exc
    if not isinstance(pending, dict):
        raise HTTPException(status_code=500, detail="pending_action格式错误")
    return pending


def expire_pending_if_needed(pending: Dict[str, Any]) -> bool:
    expires_at_raw = pending.get("expires_at")
    if not expires_at_raw:
        return False
    try:
        expired = datetime.fromisoformat(str(expires_at_raw)) < datetime.now()
    except ValueError:
        expired = True
    if expired and pending.get("status") == "pending":
        pending["status"] = "expired"
        pending["requires_confirmation"] = False
        pending["expired_at"] = utc_now_iso()
        save_pending(pending)
    return expired


def require_open_pending(pending: Dict[str, Any]) -> None:
    if pending.get("status") != "pending":
        raise HTTPException(status_code=409, detail=f"pending_action已结束：{pending.get('status')}")
    if expire_pending_if_needed(pending):
        raise HTTPException(status_code=409, detail="pending_action已过期")


def reconcile_pending_with_operations(pending: Dict[str, Any]) -> Dict[str, Any]:
    """Repair pending status from durable operation history when possible."""
    pending_id = str(pending.get("pending_id") or "").strip()
    if not pending_id:
        return pending
    operation = PortfolioWriteService().find_confirm_operation_by_pending_id(pending_id)
    if not operation:
        return pending

    changed = False
    operation_id = str(operation.get("operation_id") or "")
    if operation.get("is_rolled_back"):
        if pending.get("status") != "rolled_back" or pending.get("operation_id") != operation_id:
            pending["status"] = "rolled_back"
            pending["requires_confirmation"] = False
            pending["operation_id"] = operation_id
            pending["reconciled_at"] = utc_now_iso()
            changed = True
    elif pending.get("status") in {"pending", "confirmed"}:
        if pending.get("status") != "confirmed" or pending.get("operation_id") != operation_id:
            pending["status"] = "confirmed"
            pending["requires_confirmation"] = False
            pending["operation_id"] = operation_id
            pending["confirmed_at"] = pending.get("confirmed_at") or operation.get("created_at") or utc_now_iso()
            pending["reconciled_at"] = utc_now_iso()
            changed = True
    if changed:
        save_pending(pending)
    return pending


def public_pending(pending: Dict[str, Any]) -> Dict[str, Any]:
    normalized_changes = []
    service = PortfolioWriteService()
    for raw_change in pending.get("changes", []):
        action_type = str(raw_change.get("action_type") or raw_change.get("action") or "add_or_update")
        if action_type in {"deposit", "withdraw", "set_cash"}:
            normalized_changes.append({
                "action_type": action_type,
                "account": str(raw_change.get("account") or "").strip(),
                "currency": str(raw_change.get("currency") or "").upper().strip(),
                "amount": to_float(raw_change.get("amount"), None),
                "note": str(raw_change.get("note") or ""),
                "source": str(raw_change.get("source") or "ai"),
            })
        else:
            normalized_changes.append(service.normalize_position(raw_change))
    return {
        "ok": True,
        "pending_id": pending.get("pending_id"),
        "summary": pending.get("summary"),
        "action_type": pending.get("action_type"),
        "changes": normalized_changes,
        "missing_fields": pending.get("missing_fields", []),
        "warnings": pending.get("warnings", []),
        "requires_confirmation": pending.get("requires_confirmation", False),
        "intent": pending.get("intent", "bookkeeping"),
        "status": pending.get("status", "pending"),
        "operation_id": pending.get("operation_id"),
        "instrument_candidates": pending.get("instrument_candidates", []),
        "revises_pending_id": pending.get("revises_pending_id"),
        "revised_to_pending_id": pending.get("revised_to_pending_id"),
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
    match = re.search(
        rf"(?:{label_pattern})\s*(?:[:：=]|改为|改成|修改为|修改成|调整为|调整成|设置为|设为|是|为)?\s*({NUMBER_TOKEN_PATTERN})",
        message,
        re.IGNORECASE,
    )
    if match:
        return parse_human_number(match.group(1))
    return None


def infer_currency(message: str) -> Optional[str]:
    lower = message.lower()
    named_tokens = (
        ("人民币元", "CNY"), ("人民币", "CNY"),
        ("港币", "HKD"), ("港元", "HKD"),
        ("美元", "USD"), ("美金", "USD"), ("美刀", "USD"),
        ("欧元", "EUR"), ("日元", "JPY"), ("英镑", "GBP"),
        ("新加坡元", "SGD"), ("新币", "SGD"), ("澳元", "AUD"),
        ("加元", "CAD"), ("瑞郎", "CHF"),
    )
    for token, currency in named_tokens:
        if token in message:
            return currency
    upper_message = message.upper()
    if "HK$" in upper_message:
        return "HKD"
    if "US$" in upper_message or "$" in message:
        return "USD"
    if "¥" in message or "￥" in message:
        return "CNY"
    compact_currency = re.search(
        r"(?:IBKR|IB|长桥|银河|尊嘉|华盛通|哈富)(CNY|RMB|HKD|USD|EUR|JPY|GBP|SGD|AUD|CAD|CHF)",
        message,
        re.IGNORECASE,
    )
    if compact_currency:
        token = compact_currency.group(1).lower()
        return "CNY" if token == "rmb" else token.upper()
    for token in ("cny", "rmb", "hkd", "usd", "eur", "jpy", "gbp", "sgd", "aud", "cad", "chf"):
        if re.search(rf"{NUMBER_TOKEN_PATTERN}\s*{token}(?![a-z])", lower, re.IGNORECASE):
            return "CNY" if token == "rmb" else token.upper()
        if re.search(rf"(?<![a-z]){token}(?![a-z])", lower):
            return "CNY" if token == "rmb" else token.upper()
    if re.search(rf"{NUMBER_TOKEN_PATTERN}\s*元", message, re.IGNORECASE):
        return "CNY"
    if "刀" in message:
        return "USD"
    return None


def normalize_account(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    account = str(value).strip()
    if not account:
        return None
    upper = account.upper()
    for alias, canonical in ACCOUNT_ALIASES.items():
        if upper == alias.upper():
            return canonical
    for canonical in COMMON_ACCOUNTS | NEW_ACCOUNT_HINTS:
        if upper == canonical.upper():
            return canonical
    return account


def infer_account(message: str, allow_single: bool = False) -> tuple[Optional[str], Optional[str]]:
    text = message.strip()

    # Prefer known broker names before generic account parsing. This also
    # handles natural prepositions such as “给IBKR入金” and “向银河转出”.
    known_account_tokens = COMMON_ACCOUNTS | NEW_ACCOUNT_HINTS | set(ACCOUNT_ALIASES)
    account_action = r"(?:买|买了|买入|新增|添加|卖|卖了|卖出|入金|出金|提现|增加|减少|转入|转出|现金|余额|人民币|港币|美元|CNY|HKD|USD|换|兑换|改|变)"
    for account in sorted(known_account_tokens, key=len, reverse=True):
        account_boundary = (
            r"(?=(?:CNY|RMB|HKD|USD|EUR|JPY|GBP|SGD|AUD|CAD|CHF)|[^A-Za-z0-9]|$)"
            if account.isascii() else ""
        )
        if re.search(
            rf"(?:^|在|从|给|往|向|到|转入|转出){re.escape(account)}{account_boundary}(?:账户)?",
            text,
            re.IGNORECASE,
        ):
            return normalize_account(account), "explicit"
        if re.search(
            rf"[\s，,。；;]{re.escape(account)}{account_boundary}(?:账户)?",
            text,
            re.IGNORECASE,
        ):
            return normalize_account(account), "explicit"
        if re.search(
            rf"{re.escape(account)}{account_boundary}(?:账户)?(?=\s*{account_action})",
            text,
            re.IGNORECASE,
        ):
            return normalize_account(account), "explicit"

    account_match = re.search(
        r"(?:账户|券商|分组)\s*(?:改为|改成|修改为|修改成|调整为|调整成|还是|为|是|:|：)\s*([A-Za-z0-9\u4e00-\u9fff]{2,12})",
        text,
        re.IGNORECASE,
    )
    if account_match:
        return normalize_account(account_match.group(1)), "explicit"

    same_match = re.search(
        r"(?:还是|同上账户|这个账户)\s*([A-Za-z0-9\u4e00-\u9fff]{2,12})",
        text,
    )
    if same_match:
        return normalize_account(same_match.group(1)), "explicit"

    if (
        allow_single
        and re.fullmatch(r"[A-Za-z0-9\u4e00-\u9fff]{2,12}", text)
        and not re.search(r"账户|券商|分组|改成|改为|修改|调整|金额|数量|成本|币种", text)
    ):
        return normalize_account(text), "single"

    prefix_match = re.match(
        r"^([A-Za-z0-9\u4e00-\u9fff]{2,12})(?:买了|买入|新增|卖了|卖出|入金|现金增加|转入|出金|现金减少|转出|提现|现金余额|账户现金)",
        text,
        re.IGNORECASE,
    )
    if prefix_match:
        prefix = prefix_match.group(1)
        looks_like_asset = bool(re.search(r"ETF|LOF|基金|股票|科技|互联|指数|债|黄金|原油", prefix, re.IGNORECASE))
        looks_like_broker = bool(re.search(r"证券|银行|资本|投资|券商", prefix)) or bool(
            re.fullmatch(r"(?:IBKR|IB|长桥|银河|尊嘉|华盛通|哈富)\d+号?", prefix, re.IGNORECASE)
        )
        if prefix not in COMMON_ASSET_WORDS and not looks_like_asset and looks_like_broker:
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

    if not name and not code:
        # Extract an unknown instrument phrase so online search can resolve it.
        # Examples: "IBKR买了2股亚马逊" / "买入100份纳指ETF" / "腾讯卖出10股".
        asset_token = r"[A-Za-z\u4e00-\u9fff][A-Za-z0-9._\-\u4e00-\u9fff]{1,29}"
        account_prefix = r"(?:长桥|哈富|IBKR|IB|盈透证券|盈透|尊嘉|华盛通|银河|富途|老虎|雪盈|中信证券)"
        patterns = [
            rf"^\s*(?:{account_prefix}\s*)?({asset_token}?)\s+{NUMBER_TOKEN_PATTERN}\s*(?:{CURRENCY_TOKEN_PATTERN})?\s+{NUMBER_TOKEN_PATTERN}\s*(?:股|股票|份|个)\s*$",
            rf"(?:买了|买入|新增|添加|卖了|卖出)\s*[0-9]+(?:\.[0-9]+)?\s*(?:股|股票|份|个)\s*({asset_token})",
            rf"(?:买了|买入|新增|添加|卖了|卖出)\s*({asset_token})\s*[0-9]+(?:\.[0-9]+)?\s*(?:股|股票|份|个)",
            rf"({asset_token}?)(?:买了|买入|新增|添加|卖了|卖出)\s*[0-9]+(?:\.[0-9]+)?\s*(?:股|股票|份|个)",
            rf"({asset_token}?)\s*[0-9]+(?:\.[0-9]+)?\s*(?:股|股票|份|个)",
        ]
        for pattern in patterns:
            match = re.search(pattern, message, re.IGNORECASE)
            if match:
                candidate = match.group(1).strip()
                candidate = re.sub(r"^(?:在)?(?:长桥|哈富|IBKR|IB|尊嘉|华盛通|银河|富途|老虎|雪盈)", "", candidate, flags=re.IGNORECASE)
                if len(candidate) >= 2:
                    name = candidate
                    break

    if "海外科技" in message and not code:
        name = "海外科技"
        asset_type = asset_type or "stock"
        warnings.append("海外科技像是基金简称或自定义标的，需要确认具体代码或基金名称")
    elif "纳指ETF" in message and not code:
        name = "纳指ETF"
        asset_type = asset_type or "stock"
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
    elif "小米" in message:
        name = "小米集团"
        code = code or "1810"
        currency = currency or "HKD"
        asset_type = asset_type or "stock"
        if "港" not in message and "HK" not in message.upper():
            warnings.append("根据“小米”推断为港股小米集团1810；请在确认写入前核对")

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
            asset_type = "stock"
        elif re.fullmatch(r"\d{4}", code) or re.fullmatch(r"[A-Z.]{1,8}", code):
            asset_type = "stock"

    if not currency and ("份" in message or "海外科技" in message) and re.search(r"\d+\s*元", message):
        currency = "CNY"
    if not asset_type and re.search(r"(?:股|股票|份|个)", message):
        asset_type = "stock"

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
    currency_mentions = re.findall(CURRENCY_TOKEN_PATTERN, message, re.IGNORECASE)
    account, _ = infer_account(message)
    has_security_unit = bool(re.search(r"(?:股|股票|份|个)", message))
    if (
        len(currency_mentions) >= 2
        and re.search(r"换成了?|换为了?|换为|换到|兑换成了?|兑换为了?|兑换为|兑成了?|换了|换", message)
    ):
        return "fx_exchange"
    if any(token in message for token in ["卖了", "卖出", "清仓", "减持", "全卖", "卖光"]) or re.search(rf"卖(?:掉)?\s*{NUMBER_TOKEN_PATTERN}", message):
        return "sell"
    if (
        any(token in message for token in ["出金", "现金减少", "转出", "提现", "取出", "提了", "取了", "转走", "划出"])
        or re.search(rf"{CURRENCY_TOKEN_PATTERN}\s*(?:余额)?\s*(?:减少了?|减了?|减掉了?|减去|扣除了?|扣了?|少了)\s*{NUMBER_TOKEN_PATTERN}", message, re.IGNORECASE)
        or (
            account is not None
            and not has_security_unit
            and re.search(rf"(?:减少了?|减了?|减掉了?|减去|扣除了?|扣了?|少了)\s*{NUMBER_TOKEN_PATTERN}", message, re.IGNORECASE)
        )
    ):
        return "withdraw"
    if (
        any(token in message for token in ["入金", "现金增加", "转入", "存入", "充值", "存了", "存进", "转进", "打入", "划入"])
        or re.search(rf"{CURRENCY_TOKEN_PATTERN}\s*(?:余额)?\s*(?:增加了?|加了?|加上了?)\s*{NUMBER_TOKEN_PATTERN}", message, re.IGNORECASE)
        or (
            account is not None
            and not has_security_unit
            and re.search(rf"(?:增加了?|加了?|加上了?)\s*{NUMBER_TOKEN_PATTERN}", message, re.IGNORECASE)
        )
    ):
        return "deposit"
    if (
        any(token in message for token in ["现金余额", "账户现金", "现金有", "有现金"])
        or (account is not None and not has_security_unit and re.search(rf"(?:余额|现金)\s*(?:是|为|有|:|：)?\s*{NUMBER_TOKEN_PATTERN}", message, re.IGNORECASE))
        or (account is not None and re.search(rf"(?:有\s*{CURRENCY_TOKEN_PATTERN}\s*{NUMBER_TOKEN_PATTERN}|有\s*{NUMBER_TOKEN_PATTERN}\s*{CURRENCY_TOKEN_PATTERN}|{CURRENCY_TOKEN_PATTERN}\s*有\s*{NUMBER_TOKEN_PATTERN})", message, re.IGNORECASE))
        or (account is not None and re.search(rf"(?:{CURRENCY_TOKEN_PATTERN}\s*(?:还)?剩(?:下)?\s*{NUMBER_TOKEN_PATTERN}|(?:还)?剩(?:下)?\s*{NUMBER_TOKEN_PATTERN}\s*{CURRENCY_TOKEN_PATTERN}|{NUMBER_TOKEN_PATTERN}\s*{CURRENCY_TOKEN_PATTERN}\s*现金)", message, re.IGNORECASE))
        or re.search(r"有\s*[+-]?[0-9]+(?:\.[0-9]+)?.*现金", message)
        or re.search(rf"{CURRENCY_TOKEN_PATTERN}\s*(?:余额)?\s*(?:变为|变成|改为|改成|调整为|调整成|设置为|设为)\s*{NUMBER_TOKEN_PATTERN}", message, re.IGNORECASE)
    ):
        return "set_cash"
    if any(token in message for token in ["买了", "买入", "新增", "添加", "持仓", "写入数据库", "帮我记上", "帮我记一下", "记一笔", "录入"]) or re.search(rf"买\s*{NUMBER_TOKEN_PATTERN}\s*(?:股|股票|份|个)", message):
        return "add_or_update"
    has_quantity = bool(re.search(rf"{NUMBER_TOKEN_PATTERN}\s*(?:股|股票|份|个)", message, re.IGNORECASE))
    has_total_amount = any(token in message for token in ["一共", "总共", "总计", "花了", "总金额", "总成本"])
    if has_quantity and has_total_amount:
        return "add_or_update"
    numeric_tokens = re.findall(NUMBER_TOKEN_PATTERN, message, re.IGNORECASE)
    has_asset_text = bool(re.search(r"[A-Za-z\u4e00-\u9fff]{2,}", message))
    if has_quantity and len(numeric_tokens) >= 2 and has_asset_text:
        return "add_or_update"
    # Concise ledger entry: "银河 比亚迪 500个 102.742元".
    # Require a known account, a known asset, quantity unit and a trailing price+currency
    # so ordinary numeric chat does not become a bookkeeping card.
    has_known_asset = any(asset.lower() in message.lower() for asset in COMMON_ASSET_WORDS)
    has_trailing_price = bool(re.search(
        rf"{NUMBER_TOKEN_PATTERN}\s*{CURRENCY_TOKEN_PATTERN}\s*$",
        message,
        re.IGNORECASE,
    ))
    if account is not None and has_known_asset and has_quantity and has_trailing_price:
        return "add_or_update"
    if looks_like_structured_bookkeeping(message):
        return "add_or_update"
    return "chat_only"


def parse_quantity(message: str) -> Optional[float]:
    value = parse_number_after(message, ("quantity", "数量"))
    if value is not None:
        return value
    match = re.search(rf"({NUMBER_TOKEN_PATTERN})\s*(?:股|份|个)", message, re.IGNORECASE)
    return parse_human_number(match.group(1), None) if match else None


def parse_amounts(message: str, quantity: Optional[float]) -> tuple[Optional[float], Optional[float], Optional[float]]:
    total_cost = parse_number_after(message, ("total_cost", "总成本", "总金额", "一共花了", "总共花了"))
    cost_price = parse_number_after(message, ("cost_price", "成本价", "均价", "成交价", "price"))
    fee = parse_number_after(message, ("fee", "手续费"))

    if total_cost is None:
        total_match = re.search(
            r"(?:(?:一共|总共|总计)\s*(?:花了|金额)?|(?:花了|总金额)\s*)"
            rf"({NUMBER_TOKEN_PATTERN})\s*(?:美元|美金|港币|港元|人民币|元|刀|USD|HKD|CNY)?",
            message,
            re.IGNORECASE,
        )
        if total_match:
            total_cost = parse_human_number(total_match.group(1), None)
    if cost_price is None and total_cost is None:
        price_match = re.search(rf"(?:均价|成本价|成交价)?\s*({NUMBER_TOKEN_PATTERN})\s*(?:一股|每股|/股|美元|美金|港币|港元|元|刀)?\s*$", message, re.IGNORECASE)
        if price_match and "花了" not in message and "总" not in message and "一共" not in message:
            cost_price = parse_human_number(price_match.group(1), None)
    if cost_price is None and total_cost is None and quantity is not None:
        quantity_match = re.search(rf"{NUMBER_TOKEN_PATTERN}\s*(?:股|股票|份|个)", message, re.IGNORECASE)
        if quantity_match:
            preceding_numbers = list(re.finditer(NUMBER_TOKEN_PATTERN, message[:quantity_match.start()], re.IGNORECASE))
            if preceding_numbers:
                cost_price = parse_human_number(preceding_numbers[-1].group(0), None)
    if cost_price is None and quantity and total_cost is not None:
        cost_price = total_cost / quantity
    if total_cost is None and quantity is not None and cost_price is not None:
        total_cost = quantity * cost_price
    return cost_price, total_cost, fee


def parse_cash_amount(message: str) -> Optional[float]:
    explicit = parse_number_after(message, ("amount", "金额", "现金余额", "账户现金", "余额", "现金"))
    if explicit is not None:
        return explicit
    patterns = [
        rf"(?:入金|出金|转入|转出|存入|提现|取出|充值|现金增加|现金减少|存了|存进|提了|取了|转进|转走|打入|划入|划出)了?\s*{CURRENCY_TOKEN_PATTERN}\s*({NUMBER_TOKEN_PATTERN})",
        rf"(?:入金|出金|转入|转出|存入|提现|取出|充值|现金增加|现金减少|存了|存进|提了|取了|转进|转走|打入|划入|划出)了?\s*({NUMBER_TOKEN_PATTERN})",
        rf"(?:增加了?|加了?|加上了?|减少了?|减了?|减掉了?|减去|扣除了?|扣了?|少了)\s*({NUMBER_TOKEN_PATTERN})",
        rf"{CURRENCY_TOKEN_PATTERN}\s*(?:余额)?\s*(?:减少了?|减了?|减掉了?|减去|扣除了?|扣了?|少了|增加了?|加了?|加上了?|变为|变成|改为|改成|调整为|调整成|设置为|设为|有|(?:还)?剩(?:下)?)\s*({NUMBER_TOKEN_PATTERN})",
        rf"(?:现金余额|账户现金|现金有|有现金|余额|现金)\s*(?:是|为|有|:|：)?\s*({NUMBER_TOKEN_PATTERN})",
        rf"有\s*{CURRENCY_TOKEN_PATTERN}\s*({NUMBER_TOKEN_PATTERN})",
        rf"(?:还)?剩(?:下)?\s*({NUMBER_TOKEN_PATTERN})\s*{CURRENCY_TOKEN_PATTERN}",
        rf"({NUMBER_TOKEN_PATTERN})\s*{CURRENCY_TOKEN_PATTERN}\s*(?:现金)?",
    ]
    for pattern in patterns:
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            return parse_human_number(match.group(1), None)
    return None


def parse_fx_exchange(message: str, account: Optional[str]) -> Optional[Dict[str, Any]]:
    exchange_word = r"(?:换成了?|换为了?|换为|换到|兑换成了?|兑换为了?|兑换为|兑成了?|换了|换)"
    amount_currency = rf"({NUMBER_TOKEN_PATTERN})\s*({CURRENCY_TOKEN_PATTERN})"
    currency_amount = rf"({CURRENCY_TOKEN_PATTERN})\s*({NUMBER_TOKEN_PATTERN})"
    patterns = [
        (rf"{amount_currency}\s*{exchange_word}\s*{amount_currency}", (1, 2, 3, 4), False, False),
        (rf"{currency_amount}\s*{exchange_word}\s*{currency_amount}", (2, 1, 4, 3), True, True),
        (rf"{amount_currency}\s*{exchange_word}\s*{currency_amount}", (1, 2, 4, 3), False, True),
        (rf"{currency_amount}\s*{exchange_word}\s*{amount_currency}", (2, 1, 3, 4), True, False),
    ]

    match = None
    indexes = None
    for pattern, group_indexes, _, _ in patterns:
        candidate = re.search(pattern, message, re.IGNORECASE)
        if candidate:
            match = candidate
            indexes = group_indexes
            break
    if match is None or indexes is None:
        return None

    source_amount = parse_human_number(match.group(indexes[0]), None)
    source_currency = normalize_currency_token(match.group(indexes[1]))
    target_amount = parse_human_number(match.group(indexes[2]), None)
    target_currency = normalize_currency_token(match.group(indexes[3]))
    if source_amount is None or target_amount is None:
        return None

    changes = [
        {
            "action_type": "withdraw",
            "account": account,
            "currency": source_currency,
            "amount": source_amount,
            "note": "fx_exchange_out",
            "source": "ai",
        },
        {
            "action_type": "deposit",
            "account": account,
            "currency": target_currency,
            "amount": target_amount,
            "note": "fx_exchange_in",
            "source": "ai",
        },
    ]
    changes = [{key: value for key, value in change.items() if value is not None} for change in changes]
    missing = collect_missing_fields(changes, "fx_exchange")
    warnings: List[str] = []
    if source_currency and target_currency and source_currency == target_currency:
        missing.append("distinct_currencies")
        warnings.append("换出币种和换入币种不能相同")
    if source_amount <= 0 or target_amount <= 0:
        missing.append("positive_amount")
        warnings.append("换出和换入金额都必须大于0")
    if source_amount > 0 and target_amount > 0 and source_currency and target_currency:
        effective_rate = target_amount / source_amount
        warnings.append(
            f"实际换汇：{source_amount:g}{source_currency}→{target_amount:g}{target_currency}；"
            f"成交比率1{source_currency}={effective_rate:.8g}{target_currency}"
        )
    warnings.append("将按你提供的实际换出/换入金额记账；总资产仍按当前实时汇率折算")
    return {
        "intent": "bookkeeping",
        "summary": "识别到1笔待确认换汇信息",
        "action_type": "fx_exchange",
        "changes": changes,
        "missing_fields": list(dict.fromkeys(missing)),
        "warnings": warnings,
    }


def collect_missing_fields(changes: List[Dict[str, Any]], action_type: str) -> List[str]:
    missing: List[str] = []
    for change in changes:
        child_action = str(change.get("action_type") or action_type)
        missing.extend(build_missing(change, child_action))
    return list(dict.fromkeys(missing))


def build_missing(change: Dict[str, Any], action_type: str) -> List[str]:
    if action_type == "chat_only":
        return []
    if action_type in {"deposit", "withdraw", "set_cash"}:
        missing = []
        if not change.get("account"):
            missing.append("account")
        currency = str(change.get("currency") or "").upper().strip()
        if not currency:
            missing.append("currency")
        elif currency not in SUPPORTED_CURRENCIES:
            missing.append("unsupported_currency")
        amount = to_float(change.get("amount"), None)
        if amount is None:
            missing.append("amount")
        elif action_type in {"deposit", "withdraw"} and amount <= 0:
            missing.append("positive_amount")
        elif action_type == "set_cash" and amount < 0:
            missing.append("amount_non_negative")
        return missing

    missing = []
    if not change.get("account"):
        missing.append("account")
    if not change.get("name") and not change.get("code"):
        missing.append("name/code")
    if not change.get("code"):
        missing.append("code")
    currency = str(change.get("currency") or "").upper().strip()
    if not currency:
        missing.append("currency")
    elif currency not in SUPPORTED_CURRENCIES:
        missing.append("unsupported_currency")
    if not change.get("asset_type"):
        missing.append("asset_type")
    quantity = to_float(change.get("quantity"), None)
    if action_type != "delete":
        if quantity is None:
            missing.append("quantity")
        elif quantity <= 0:
            missing.append("positive_quantity")
    if action_type not in {"sell", "delete"}:
        cost_price = to_float(change.get("cost_price"), None)
        total_cost = to_float(change.get("total_cost"), None)
        if cost_price is None and total_cost is None:
            missing.append("cost_price")
        elif cost_price is not None and cost_price < 0:
            missing.append("cost_price_non_negative")
        elif cost_price is None and total_cost is not None and total_cost < 0:
            missing.append("cost_price_non_negative")
    return missing


def expected_currency_for_code(code: Any) -> Optional[str]:
    clean_code, inferred_currency, _ = strip_code_prefix(code)
    if inferred_currency:
        return inferred_currency
    if clean_code.isdigit() and len(clean_code) == 4:
        return "HKD"
    if clean_code.isdigit() and len(clean_code) == 6:
        return "CNY"
    if clean_code and clean_code.isascii() and not clean_code.isdigit():
        return "USD"
    return None


def enrich_instrument_currency_consistency(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """Block code/currency combinations that cannot be quoted consistently."""
    if parsed.get("action_type") in {"deposit", "withdraw", "set_cash", "fx_exchange", "multi_cash"}:
        return parsed
    if not parsed.get("changes"):
        return parsed

    warnings = [warning for warning in parsed.get("warnings", []) if "代码通常使用" not in warning]
    missing = [item for item in parsed.get("missing_fields", []) if item != "currency_conflict"]
    for change in parsed.get("changes", []):
        code = str(change.get("code") or "").strip()
        currency = str(change.get("currency") or "").upper().strip()
        expected = expected_currency_for_code(code)
        if expected and currency and currency != expected:
            missing.append("currency_conflict")
            warnings.append(
                f"代码{code}通常使用{expected}，但当前币种是{currency}；请修改币种或代码"
            )

    result = dict(parsed)
    result["missing_fields"] = list(dict.fromkeys(missing))
    result["warnings"] = list(dict.fromkeys(warnings))
    return result


def parse_bookkeeping_message(
    message: str,
    previous: Optional[Dict[str, Any]] = None,
    _allow_multi: bool = True,
) -> Dict[str, Any]:
    service = PortfolioWriteService()
    existing_positions = compact_positions()
    text = unicodedata.normalize("NFKC", str(message or "")).strip()

    if previous and previous.get("changes") and previous.get("missing_fields") in (["account"], ["account/group"]):
        account, _ = infer_account(text, allow_single=True)
        if account:
            action_type = str(previous.get("action_type") or "add_or_update")
            updated_changes: List[Dict[str, Any]] = []
            for raw_change in previous["changes"]:
                updated = raw_change.copy()
                updated["account"] = account
                updated["updated_at"] = utc_now_iso()
                child_action = str(updated.get("action_type") or action_type)
                if child_action in {"deposit", "withdraw", "set_cash"}:
                    updated_changes.append(updated)
                else:
                    updated_changes.append(service.normalize_position(updated))

            warnings = [
                warning for warning in previous.get("warnings", [])
                if "新账户分组" not in warning
                and "检测到多个账户持有该标的" not in warning
                and "当前账户中未找到该持仓" not in warning
            ]
            if account not in COMMON_ACCOUNTS:
                warnings.append(f"{account} 是新账户分组，后续确认写入时可创建/使用")
            missing = collect_missing_fields(updated_changes, action_type)
            if action_type == "sell":
                updated = updated_changes[0]
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
                    original_message = str(previous.get("message") or "")
                    if re.search(r"清仓|全部|全卖|卖光", original_message):
                        updated["quantity"] = available
                        updated["available_qty"] = available
                        updated_changes[0] = service.normalize_position(updated)
                        missing = collect_missing_fields(updated_changes, action_type)
                        warnings.append(f"已按账户{account}现有持仓数量{available:g}执行全部卖出")
                    else:
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
                "action_type": action_type,
                "changes": updated_changes,
                "missing_fields": missing,
                "warnings": warnings,
            }

    if non_actionable_statement(text):
        return {
            "intent": "chat_only",
            "summary": "检测到否定、假设或操作咨询，不创建记账确认卡",
            "action_type": "chat_only",
            "changes": [],
            "missing_fields": [],
            "warnings": [],
        }

    action_probe = infer_action(text)
    invalid_number = malformed_number_reason(text)
    if action_probe != "chat_only" and invalid_number:
        changes: List[Dict[str, Any]] = []
        missing = ["invalid_number"]
        if action_probe in {"deposit", "withdraw", "set_cash"}:
            account, _ = infer_account(text)
            currency = (parse_first_key_value(text, ("currency", "币种")) or infer_currency(text) or "").upper() or None
            if currency is None and account == "银河":
                currency = "CNY"
            change = {
                "action_type": action_probe,
                "account": account,
                "currency": currency,
                "note": "",
                "source": "ai",
            }
            change = {key: value for key, value in change.items() if value is not None}
            changes = [change]
            missing = list(dict.fromkeys(build_missing(change, action_probe) + ["invalid_number"]))
        return {
            "intent": "bookkeeping",
            "summary": "识别到记账意图，但数字格式无效",
            "action_type": action_probe,
            "changes": changes,
            "missing_fields": missing,
            "warnings": [invalid_number],
        }

    if previous is None and _allow_multi:
        clauses = split_user_clauses(text)
        if len(clauses) > 1:
            parsed_clauses = [
                parse_bookkeeping_message(clause, _allow_multi=False)
                for clause in clauses
            ]
            actionable = [parsed for parsed in parsed_clauses if parsed.get("intent") == "bookkeeping"]
            if len(actionable) == 1:
                return actionable[0]
            if len(actionable) > 1:
                cash_actions = {"deposit", "withdraw", "set_cash"}
                if all(parsed.get("action_type") in cash_actions and len(parsed.get("changes", [])) == 1 for parsed in actionable):
                    combined_changes: List[Dict[str, Any]] = []
                    combined_warnings: List[str] = []
                    inherited_account: Optional[str] = None
                    for parsed in actionable:
                        change = dict(parsed["changes"][0])
                        if not change.get("account") and inherited_account:
                            change["account"] = inherited_account
                        if change.get("account"):
                            inherited_account = str(change["account"])
                        combined_changes.append(change)
                        combined_warnings.extend(parsed.get("warnings", []))
                    return {
                        "intent": "bookkeeping",
                        "summary": f"识别到{len(combined_changes)}条待确认账户现金信息",
                        "action_type": "multi_cash",
                        "changes": combined_changes,
                        "missing_fields": collect_missing_fields(combined_changes, "multi_cash"),
                        "warnings": list(dict.fromkeys(combined_warnings + ["多条现金变化将作为同一个原子操作一起写入或一起失败"])),
                    }
                return {
                    "intent": "bookkeeping",
                    "summary": "一条消息中识别到多笔不同类型的操作",
                    "action_type": "multiple_operations",
                    "changes": [],
                    "missing_fields": ["multiple_operations"],
                    "warnings": ["为避免漏记或错配，请将不同类型的交易拆成多条消息提交"],
                }

    action_type = action_probe
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
    if action_type == "fx_exchange":
        parsed_exchange = parse_fx_exchange(text, account)
        if parsed_exchange is not None:
            if account and account not in COMMON_ACCOUNTS and account_source in {"candidate", "single", "explicit"}:
                parsed_exchange["warnings"].append(
                    f"{account} 是新账户分组，后续确认写入时可创建/使用"
                )
            return parsed_exchange
        return {
            "intent": "bookkeeping",
            "summary": "识别到换汇意图，但未完整识别两种币种和金额",
            "action_type": "fx_exchange",
            "changes": [],
            "missing_fields": ["source_amount/currency", "target_amount/currency"],
            "warnings": ["请使用类似“长桥500港币换成20美元”的表达"],
        }

    if action_type in {"deposit", "withdraw", "set_cash"}:
        currency = (parse_first_key_value(text, ("currency", "币种")) or infer_currency(text) or "").upper() or None
        if currency is None and account == "银河":
            currency = "CNY"
        amount = parse_cash_amount(text)
        warnings: List[str] = []
        if account and account not in COMMON_ACCOUNTS and account_source in {"candidate", "single", "explicit"}:
            warnings.append(f"{account} 是新账户分组，后续确认写入时可创建/使用")
        change = {
            "action_type": action_type,
            "account": account,
            "currency": currency,
            "amount": amount,
            "note": "",
            "source": "ai",
        }
        change = {key: value for key, value in change.items() if value is not None}
        return {
            "intent": "bookkeeping",
            "summary": "识别到1条待确认账户现金信息",
            "action_type": action_type,
            "changes": [change],
            "missing_fields": build_missing(change, action_type),
            "warnings": warnings,
        }

    asset, warnings = infer_asset(text, existing_positions)
    quantity = parse_quantity(text)
    cost_price, total_cost, fee = parse_amounts(text, quantity)

    sell_matches: List[Dict[str, Any]] = []
    if action_type == "sell":
        account, sell_matches = resolve_sell_account(
            account, asset, existing_positions, warnings
        )
        if account and sell_matches and re.search(r"清仓|全部|全卖|卖光", text):
            selected = [
                position for position in sell_matches
                if str(position.get("account") or "").strip() == account
            ]
            if selected:
                quantity = sum(to_float(position.get("quantity"), 0.0) or 0.0 for position in selected)
                warnings.append(f"已按账户{account}现有持仓数量{quantity:g}执行全部卖出")

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

    return enrich_instrument_currency_consistency({
        "intent": "bookkeeping",
        "summary": "识别到1条待确认记账信息",
        "action_type": action_type,
        "changes": [change],
        "missing_fields": missing,
        "warnings": sorted(set(warnings)),
    })


def enrich_cash_availability(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """Block cash reductions/exchanges that would make a balance negative."""
    if parsed.get("action_type") not in {"withdraw", "fx_exchange", "multi_cash"}:
        return parsed
    if not parsed.get("changes"):
        return parsed

    portfolio = PortfolioWriteService().load_portfolio()
    balances = {
        (str(item.get("account") or "").strip(), str(item.get("currency") or "").upper().strip()):
        to_float(item.get("amount"), 0.0) or 0.0
        for item in portfolio.get("cash_accounts", [])
    }
    warnings = [
        warning for warning in parsed.get("warnings", [])
        if "现金不足" not in warning
    ]
    missing = [item for item in parsed.get("missing_fields", []) if item != "available_cash"]

    for change in parsed.get("changes", []):
        action = str(change.get("action_type") or parsed.get("action_type") or "")
        account = str(change.get("account") or "").strip()
        currency = str(change.get("currency") or "").upper().strip()
        amount = to_float(change.get("amount"), None)
        if not account or not currency or amount is None:
            continue
        identity = (account, currency)
        current = balances.get(identity, 0.0)
        if action == "deposit":
            balances[identity] = current + amount
        elif action == "set_cash":
            balances[identity] = amount
        elif action == "withdraw":
            if amount > current + 1e-8:
                missing.append("available_cash")
                warnings.append(
                    f"{account}/{currency}现金不足：当前{current:g}，需要减少{amount:g}"
                )
            else:
                balances[identity] = current - amount

    result = dict(parsed)
    result["missing_fields"] = list(dict.fromkeys(missing))
    result["warnings"] = list(dict.fromkeys(warnings))
    return result


def enrich_sell_availability(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """Revalidate sell revisions against the latest portfolio state."""
    if parsed.get("action_type") != "sell" or not parsed.get("changes"):
        return parsed

    change = dict(parsed["changes"][0])
    positions = compact_positions()
    warnings = [
        warning for warning in parsed.get("warnings", [])
        if "暂不支持卖空" not in warning
        and "超过账户" not in warning
        and "检测到多个账户持有" not in warning
        and "自动选择账户" not in warning
    ]
    missing = [
        item for item in build_missing(change, "sell")
        if item not in {"existing_position", "available_quantity"}
    ]
    matches = find_position_matches(change, positions)
    account = str(change.get("account") or "").strip()

    if not account:
        accounts = sorted({
            str(position.get("account") or "").strip()
            for position in matches
            if str(position.get("account") or "").strip()
        })
        if len(accounts) == 1:
            account = accounts[0]
            change["account"] = account
            missing = [item for item in missing if item != "account"]
            warnings.append(f"根据现有持仓自动选择账户：{account}")
        elif len(accounts) > 1:
            if "account" not in missing:
                missing.append("account")
            warnings.append(f"检测到多个账户持有该标的：{'、'.join(accounts)}，请选择卖出账户")
        else:
            missing.append("existing_position")
            warnings.append("当前账户中未找到该持仓，暂不支持卖空")
    else:
        selected = [
            position for position in matches
            if str(position.get("account") or "").strip() == account
        ]
        if not selected:
            missing.append("existing_position")
            warnings.append(f"账户{account}中未找到该持仓，暂不支持卖空")
        else:
            available = sum(to_float(position.get("quantity"), 0.0) or 0.0 for position in selected)
            quantity = to_float(change.get("quantity"), None)
            if quantity is not None and quantity > available + 1e-8:
                missing.append("available_quantity")
                warnings.append(f"卖出数量{quantity:g}超过账户{account}现有持仓{available:g}，暂不支持卖空")

    result = dict(parsed)
    result["changes"] = [change]
    result["missing_fields"] = list(dict.fromkeys(missing))
    result["warnings"] = list(dict.fromkeys(warnings))
    return result


def instrument_search_keywords(keyword: str) -> List[str]:
    raw = str(keyword or "").strip()
    if not raw:
        return []
    normalized = raw.replace("纳之", "纳指")
    if "纳指" in normalized:
        normalized = normalized.replace("达成", "大成")

    keywords: List[str] = []
    # “纳指大成/纳指达成” refers to the exchange-traded ETF, not its
    # off-exchange A/C feeder funds. Put the listed-security query first.
    if "大成" in normalized and ("纳指" in normalized or "纳斯达克" in normalized):
        keywords.extend(["纳斯达克100ETF 大成", "159513"])
    keywords.extend([normalized, raw])
    return list(dict.fromkeys(keyword for keyword in keywords if keyword))


async def enrich_with_online_instrument_search(
    parsed: Dict[str, Any],
    message: str,
) -> Dict[str, Any]:
    """Resolve a missing code from an instrument name before asking the user."""
    if parsed.get("intent") != "bookkeeping" or not parsed.get("changes"):
        return parsed
    if parsed.get("action_type") in {"deposit", "withdraw", "set_cash", "fx_exchange"}:
        return parsed

    change = dict(parsed["changes"][0])
    if change.get("code"):
        return parsed
    keyword = str(change.get("name") or "").strip()
    if not keyword:
        return parsed

    candidates: List[Dict[str, Any]] = []
    search_error: Optional[Exception] = None
    for search_keyword in instrument_search_keywords(keyword):
        try:
            candidates = await InstrumentSearchService().search(search_keyword, limit=8)
        except Exception as exc:
            search_error = exc
            continue
        candidates = [
            candidate for candidate in candidates
            if str(candidate.get("classify") or "") != "OTCFUND"
        ]
        if candidates:
            break
    if not candidates:
        if search_error is not None:
            warnings = list(parsed.get("warnings", []))
            warnings.append(f"标的在线搜索暂时失败：{search_error}")
            parsed["warnings"] = list(dict.fromkeys(warnings))
        return parsed

    chosen = InstrumentSearchService.choose_confident(candidates)
    warnings = [
        warning for warning in parsed.get("warnings", [])
        if "需要确认具体代码" not in warning
        and "需要确认具体代码或基金名称" not in warning
        and "像是基金简称或自定义标的" not in warning
    ]

    if chosen:
        change["code"] = chosen["code"]
        change["name"] = chosen["name"]
        change["currency"] = change.get("currency") or chosen["currency"]
        change["asset_type"] = chosen["asset_type"]
        change = PortfolioWriteService().normalize_position(change)
        parsed["changes"] = [change]
        parsed["missing_fields"] = build_missing(change, str(parsed.get("action_type") or "add_or_update"))
        warnings.append(
            f"已联网匹配：{chosen['code']} {chosen['name']}；请在确认写入前核对"
        )
        parsed["warnings"] = list(dict.fromkeys(warnings))
        parsed["instrument_candidates"] = candidates[:5]
        return enrich_instrument_currency_consistency(parsed)

    parsed["instrument_candidates"] = candidates[:5]
    preview = "、".join(f"{item['code']} {item['name']}" for item in candidates[:5])
    warnings.append(f"联网找到多个候选：{preview}。请补充具体代码")
    parsed["warnings"] = list(dict.fromkeys(warnings))
    return parsed


def apply_direct_code_revision(
    pending: Dict[str, Any],
    message: str,
) -> Dict[str, Any] | None:
    """Allow a bare code or '代码xxxx' to select an online-search candidate."""
    if not pending.get("changes"):
        return None
    match = re.search(
        r"^(?:代码|code)?\s*[:：]?\s*([A-Za-z]{1,8}|[0-9]{4,6})\s*$",
        message.strip(),
        re.IGNORECASE,
    )
    if not match:
        return None
    code = match.group(1).upper()
    candidates = pending.get("instrument_candidates", []) or []
    selected = next((candidate for candidate in candidates if str(candidate.get("code") or "").upper() == code), None)

    updated = dict(pending["changes"][0])
    updated["code"] = code
    if selected:
        updated["name"] = selected.get("name") or updated.get("name")
        updated["currency"] = updated.get("currency") or selected.get("currency")
        updated["asset_type"] = selected.get("asset_type") or updated.get("asset_type")
    updated = PortfolioWriteService().normalize_position(updated)
    action_type = str(pending.get("action_type") or "add_or_update")
    missing = build_missing(updated, action_type)
    warnings = [warning for warning in pending.get("warnings", []) if "联网找到多个候选" not in warning]
    if selected:
        warnings.append(f"已选择联网候选：{selected['code']} {selected['name']}")
    return enrich_instrument_currency_consistency({
        "intent": "bookkeeping",
        "summary": "已补充标的代码",
        "action_type": action_type,
        "changes": [updated],
        "missing_fields": missing,
        "warnings": list(dict.fromkeys(warnings)),
        "instrument_candidates": candidates,
    })


def parse_currency_revision(message: str) -> Optional[str]:
    text = re.sub(r"[\s，,。；;:：]", "", str(message or "")).upper()
    aliases = {
        "人民币": "CNY", "人民币元": "CNY", "元": "CNY", "RMB": "CNY", "CNY": "CNY",
        "港币": "HKD", "港元": "HKD", "HKD": "HKD",
        "美元": "USD", "美金": "USD", "美刀": "USD", "刀": "USD", "USD": "USD",
        "欧元": "EUR", "EUR": "EUR", "日元": "JPY", "JPY": "JPY",
        "英镑": "GBP", "GBP": "GBP", "新加坡元": "SGD", "新币": "SGD", "SGD": "SGD",
        "澳元": "AUD", "AUD": "AUD", "加元": "CAD", "CAD": "CAD", "瑞郎": "CHF", "CHF": "CHF",
    }
    changed = True
    while changed:
        changed = False
        for prefix in ("币种", "改成", "改为", "修改为", "修改成", "变成", "变为", "设置为", "设为", "调整为", "调整成"):
            if text.startswith(prefix):
                text = text[len(prefix):]
                changed = True
                break
    return aliases.get(text)


def apply_direct_field_revision(
    pending: Dict[str, Any],
    message: str,
) -> Optional[Dict[str, Any]]:
    """Apply a concise field-only revision without rebuilding the whole record.

    Examples: 港币、币种HKD、金额3000、数量100、成本价20、长桥。
    The result is still a new pending action and must be confirmed separately.
    """
    if not pending.get("changes"):
        return None

    action_type = str(pending.get("action_type") or "add_or_update")
    original_changes = [dict(change) for change in pending.get("changes", [])]
    warnings = list(pending.get("warnings", []))
    text = unicodedata.normalize("NFKC", str(message or "")).strip()
    if "不是" in text:
        correction = re.search(r"(?:而是|[,，]\s*是|;\s*是|；\s*是)\s*(.+?)\s*$", text)
        if correction:
            text = correction.group(1).strip()

    if action_type == "multi_cash":
        return None

    if action_type in {"deposit", "withdraw", "set_cash"}:
        invalid_number = malformed_number_reason(text)
        if invalid_number:
            parsed = {
                "intent": "bookkeeping",
                "summary": "修改内容中的数字格式无效",
                "action_type": action_type,
                "changes": original_changes,
                "missing_fields": list(dict.fromkeys(collect_missing_fields(original_changes, action_type) + ["invalid_number"])),
                "warnings": list(dict.fromkeys(warnings + [invalid_number])),
                "instrument_candidates": pending.get("instrument_candidates", []),
            }
            return parsed

        currency = parse_currency_revision(text)
        amount = parse_number_after(text, ("amount", "金额", "余额", "现金"))
        compact_match = re.fullmatch(
            rf"\s*(?:(?:金额|余额|现金)\s*)?(?:(?:改成|改为|修改为|修改成|调整为|调整成|设置为|设为|变成|变为)\s*)?({NUMBER_TOKEN_PATTERN})\s*(?:({CURRENCY_TOKEN_PATTERN}))?\s*[。.]?\s*",
            text,
            re.IGNORECASE,
        )
        if compact_match:
            amount = parse_human_number(compact_match.group(1), amount)
            currency = normalize_currency_token(compact_match.group(2)) or currency
        elif amount is not None:
            currency = infer_currency(text) or currency

        if amount is not None or currency is not None:
            change = original_changes[0]
            if amount is not None:
                warnings = [
                    warning for warning in warnings
                    if "科学计数法" not in warning
                    and "数字中包含多个小数点" not in warning
                    and "千分位逗号格式不正确" not in warning
                    and "复合金额" not in warning
                ]
            changes_summary: List[str] = []
            if amount is not None:
                change["amount"] = amount
                changes_summary.append(f"金额{format_human_number(amount)}")
            if currency is not None:
                change["currency"] = currency
                changes_summary.append(f"币种{currency}")
            change["updated_at"] = utc_now_iso()
            parsed = {
                "intent": "bookkeeping",
                "summary": "已修改现金信息，请重新确认",
                "action_type": action_type,
                "changes": original_changes,
                "missing_fields": collect_missing_fields(original_changes, action_type),
                "warnings": list(dict.fromkeys(warnings + [f"已修改：{'、'.join(changes_summary)}"])),
                "instrument_candidates": pending.get("instrument_candidates", []),
            }
            return enrich_cash_availability(parsed)

    currency = parse_currency_revision(text)
    if currency:
        if action_type == "fx_exchange" and len(original_changes) > 1:
            return None
        for change in original_changes:
            change["currency"] = currency
            change["updated_at"] = utc_now_iso()
        warnings.append(f"已将币种修改为{currency}")
        return {
            "intent": "bookkeeping",
            "summary": "已修改币种，请重新确认",
            "action_type": action_type,
            "changes": original_changes,
            "missing_fields": collect_missing_fields(original_changes, action_type),
            "warnings": list(dict.fromkeys(warnings)),
            "instrument_candidates": pending.get("instrument_candidates", []),
        }

    account, account_source = infer_account(text, allow_single=True)
    if account is None:
        known_accounts = sorted(COMMON_ACCOUNTS | NEW_ACCOUNT_HINTS | set(ACCOUNT_ALIASES), key=len, reverse=True)
        for candidate in known_accounts:
            if re.fullmatch(rf"(?:改成|改为|修改为|修改成|调整为|调整成)?\s*{re.escape(candidate)}", text, re.IGNORECASE):
                account = normalize_account(candidate)
                account_source = "explicit"
                break
    if account and (
        account in COMMON_ACCOUNTS
        or account in NEW_ACCOUNT_HINTS
        or account_source == "explicit"
        or re.search(r"账户|券商|分组", text)
    ):
        for change in original_changes:
            change["account"] = account
            change["updated_at"] = utc_now_iso()
        warnings = [warning for warning in warnings if "新账户分组" not in warning]
        if account not in COMMON_ACCOUNTS:
            warnings.append(f"{account}是新账户分组，请在确认写入前核对")
        if action_type == "sell" and re.search(r"清仓|全部|全卖|卖光", str(pending.get("message") or "")):
            selected = [
                position for position in find_position_matches(original_changes[0], compact_positions())
                if str(position.get("account") or "").strip() == account
            ]
            if selected:
                available = sum(to_float(position.get("quantity"), 0.0) or 0.0 for position in selected)
                original_changes[0]["quantity"] = available
                original_changes[0]["available_qty"] = available
                warnings.append(f"已按账户{account}现有持仓数量{available:g}执行全部卖出")
        parsed = {
            "intent": "bookkeeping",
            "summary": "已修改账户，请重新确认",
            "action_type": action_type,
            "changes": original_changes,
            "missing_fields": collect_missing_fields(original_changes, action_type),
            "warnings": list(dict.fromkeys(warnings)),
            "instrument_candidates": pending.get("instrument_candidates", []),
        }
        return enrich_cash_availability(parsed)

    quantity = parse_number_after(text, ("quantity", "数量"))
    if quantity is not None and action_type not in {"deposit", "withdraw", "set_cash", "fx_exchange"}:
        change = original_changes[0]
        change["quantity"] = quantity
        cost_price = to_float(change.get("cost_price"), None)
        if cost_price is not None:
            change["total_cost"] = quantity * cost_price
        change["updated_at"] = utc_now_iso()
        return {
            "intent": "bookkeeping",
            "summary": "已修改数量，请重新确认",
            "action_type": action_type,
            "changes": original_changes,
            "missing_fields": collect_missing_fields(original_changes, action_type),
            "warnings": list(dict.fromkeys(warnings + [f"已将数量修改为{quantity:g}"])),
            "instrument_candidates": pending.get("instrument_candidates", []),
        }

    cost_price = parse_number_after(text, ("cost_price", "成本价", "均价", "成交价"))
    if cost_price is not None and action_type not in {"deposit", "withdraw", "set_cash", "fx_exchange"}:
        change = original_changes[0]
        change["cost_price"] = cost_price
        quantity = to_float(change.get("quantity"), None)
        if quantity is not None:
            change["total_cost"] = quantity * cost_price
        change["updated_at"] = utc_now_iso()
        return {
            "intent": "bookkeeping",
            "summary": "已修改成本价，请重新确认",
            "action_type": action_type,
            "changes": original_changes,
            "missing_fields": collect_missing_fields(original_changes, action_type),
            "warnings": list(dict.fromkeys(warnings + [f"已将成本价修改为{cost_price:g}"])),
            "instrument_candidates": pending.get("instrument_candidates", []),
        }

    return None


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
        "instrument_candidates": parsed.get("instrument_candidates", []),
        "requires_confirmation": parsed.get("intent") == "bookkeeping" and len(missing) == 0,
    }
    return pending


@router.post("/ai-preview")
async def ai_preview(request: PreviewRequest):
    parsed = parse_bookkeeping_message(request.message)
    parsed = await enrich_with_online_instrument_search(parsed, request.message)
    parsed = enrich_cash_availability(parsed)
    parsed = enrich_sell_availability(parsed)
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
    with PORTFOLIO_MUTATION_LOCK:
        current = load_pending(request.pending_id)
        if current.get("status") == "superseded" and current.get("revised_to_pending_id"):
            successor = load_pending(str(current["revised_to_pending_id"]))
            revision_marker = f"[revise] {request.message}"
            if revision_marker in str(successor.get("message") or ""):
                return public_pending(successor)
            raise HTTPException(status_code=409, detail="该确认卡已被另一条修改替代")
        require_open_pending(current)
        pending = deepcopy(current)
        revision_token = current.get("updated_at") or current.get("created_at")

    parsed = apply_direct_field_revision(pending, request.message)
    if parsed is None:
        parsed = apply_direct_code_revision(pending, request.message)
    if parsed is None:
        parsed = parse_bookkeeping_message(request.message, previous=pending)
        parsed = await enrich_with_online_instrument_search(parsed, request.message)
    parsed = enrich_cash_availability(parsed)
    parsed = enrich_sell_availability(parsed)
    if parsed.get("intent") == "chat_only":
        raise HTTPException(status_code=400, detail="未识别到可用于补充的记账信息")

    with PORTFOLIO_MUTATION_LOCK:
        latest = load_pending(request.pending_id)
        require_open_pending(latest)
        latest_token = latest.get("updated_at") or latest.get("created_at")
        if latest_token != revision_token:
            raise HTTPException(status_code=409, detail="该确认卡已被其他修改更新，请刷新后重试")

        combined_message = f"{latest.get('message', '')}\n[revise] {request.message}".strip()
        successor = make_pending(
            parsed,
            str(latest.get("input_type") or "text"),
            combined_message,
        )
        successor["revises_pending_id"] = latest["pending_id"]
        successor["updated_at"] = utc_now_iso()
        save_pending(successor)

        try:
            latest["status"] = "superseded"
            latest["requires_confirmation"] = False
            latest["revised_to_pending_id"] = successor["pending_id"]
            latest["updated_at"] = utc_now_iso()
            save_pending(latest)
        except Exception:
            pending_path(successor["pending_id"]).unlink(missing_ok=True)
            raise
        return public_pending(successor)


@router.post("/ai-confirm")
async def ai_confirm(request: ConfirmRequest):
    refresh_quotes = False
    with PORTFOLIO_MUTATION_LOCK:
        pending = load_pending(request.pending_id)
        service = PortfolioWriteService()

        if pending.get("status") == "confirmed":
            operation_id = str(pending.get("operation_id") or "").strip()
            existing = service.find_confirm_operation_by_pending_id(request.pending_id)
            if existing and existing.get("is_rolled_back"):
                pending["status"] = "rolled_back"
                pending["requires_confirmation"] = False
                pending["operation_id"] = existing.get("operation_id")
                save_pending(pending)
                raise HTTPException(status_code=409, detail="该确认卡对应的写入已经撤回")
            if not operation_id and existing:
                operation_id = str(existing.get("operation_id") or "")
                pending["operation_id"] = operation_id
                save_pending(pending)
            if operation_id:
                return {
                    "ok": True,
                    "operation_id": operation_id,
                    "imported_positions": (existing or {}).get("imported_positions", 0),
                    "backup_path": (existing or {}).get("backup_path", ""),
                    "portfolio_updated": False,
                    "already_confirmed": True,
                    "quote_refresh": {"ok": True, "refreshed": False},
                }
            raise HTTPException(status_code=409, detail="该确认卡已确认，但缺少operation记录")

        require_open_pending(pending)
        if pending.get("missing_fields"):
            raise HTTPException(status_code=400, detail=f"仍有缺失字段: {', '.join(pending['missing_fields'])}")

        existing = service.find_confirm_operation_by_pending_id(request.pending_id)
        if existing and existing.get("is_rolled_back"):
            pending["status"] = "rolled_back"
            pending["requires_confirmation"] = False
            pending["operation_id"] = existing.get("operation_id")
            save_pending(pending)
            raise HTTPException(status_code=409, detail="该确认卡对应的写入已经撤回")
        if existing:
            pending["status"] = "confirmed"
            pending["requires_confirmation"] = False
            pending["confirmed_at"] = pending.get("confirmed_at") or utc_now_iso()
            pending["operation_id"] = existing["operation_id"]
            save_pending(pending)
            return {
                "ok": True,
                "operation_id": existing["operation_id"],
                "imported_positions": existing.get("imported_positions", 0),
                "backup_path": existing.get("backup_path", ""),
                "portfolio_updated": False,
                "already_confirmed": True,
                "quote_refresh": {"ok": True, "refreshed": False},
            }

        for change in pending.get("changes", []):
            missing = service.validate_confirmable_change(change)
            if missing:
                raise HTTPException(status_code=400, detail=f"无法确认，字段无效: {', '.join(missing)}")

        try:
            result = service.safe_add_positions(
                pending.get("changes", []),
                summary=pending.get("summary", "AI确认写入"),
                pending_action=pending,
            )
        except (ValueError, FileNotFoundError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        pending["status"] = "confirmed"
        pending["requires_confirmation"] = False
        pending["confirmed_at"] = utc_now_iso()
        pending["operation_id"] = result["operation_id"]
        save_pending(pending)
        refresh_quotes = any(
            str(change.get("action_type") or change.get("action") or "add_or_update")
            not in {"deposit", "withdraw", "set_cash"}
            for change in pending.get("changes", [])
        )

    refresh_result = await reload_and_refresh_portfolio_provider(refresh_quotes=refresh_quotes)
    result["quote_refresh"] = refresh_result
    return result


@router.post("/ai-cancel")
async def ai_cancel(request: CancelRequest):
    with PORTFOLIO_MUTATION_LOCK:
        pending = load_pending(request.pending_id)
        if pending.get("status") == "cancelled":
            return {
                "ok": True,
                "pending_id": request.pending_id,
                "status": "cancelled",
                "already_cancelled": True,
            }
        require_open_pending(pending)
        pending["status"] = "cancelled"
        pending["requires_confirmation"] = False
        pending["cancelled_at"] = utc_now_iso()
        save_pending(pending)
        return {"ok": True, "pending_id": request.pending_id, "status": "cancelled"}


@router.get("/pending/{pending_id}")
async def get_pending(pending_id: str):
    with PORTFOLIO_MUTATION_LOCK:
        pending = load_pending(pending_id)
        pending = reconcile_pending_with_operations(pending)
        expire_pending_if_needed(pending)
        return public_pending(pending)


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
    result["quote_refresh"] = await reload_and_refresh_portfolio_provider(refresh_quotes=True)
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
    result["quote_refresh"] = await reload_and_refresh_portfolio_provider(refresh_quotes=True)
    return result


async def reload_and_refresh_portfolio_provider(refresh_quotes: bool = False) -> Dict[str, Any]:
    try:
        from backend.main import get_agent_service

        service = get_agent_service()
        if service is None:
            return {"ok": False, "error": "服务尚未初始化"}
        service._init_portfolio_provider()
        if refresh_quotes:
            return await service.refresh_portfolio()
        return {"ok": True, "refreshed": False}
    except Exception as exc:
        print(f"[PortfolioAI] 持仓provider重载/刷新失败: {exc}")
        return {"ok": False, "error": str(exc)}


def reload_agent_portfolio_provider() -> None:
    try:
        from backend.main import get_agent_service

        service = get_agent_service()
        if service is not None:
            service._init_portfolio_provider()
    except Exception as exc:
        print(f"[PortfolioAI] 持仓 provider 重载失败: {exc}")
