"""
Canonical portfolio schema and safe write helpers.

Stored positions use one user-facing identity only:
    account + code + currency

Legacy input aliases (group/symbol) are accepted at boundaries, but are never
written back to portfolio.json. market/exchange/canonical_symbol are discarded.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
from threading import RLock
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4


DATA_DIR = Path("data")
PORTFOLIO_FILE = DATA_DIR / "portfolio.json"
BACKUPS_DIR = DATA_DIR / "backups"
OPERATIONS_DIR = DATA_DIR / "operations"
PENDING_DIR = DATA_DIR / "pending_actions"

PROHIBITED_FIELDS = {
    "market",
    "exchange",
    "canonical_symbol",
}

# New writes persist only canonical bookkeeping fields plus the temporary quote
# compatibility fields still used by the existing dashboard.
POSITION_STORAGE_FIELDS = {
    "account",
    "name",
    "code",
    "currency",
    "asset_type",
    "quantity",
    "cost_price",
    "total_cost",
    "fee",
    "note",
    "source",
    "created_at",
    "updated_at",
    "available_qty",
    "current_price",
    "side",
}

PositionIdentity = Tuple[str, str, str]
SUPPORTED_CURRENCIES = {"CNY", "USD", "HKD"}
ACCOUNT_CANONICAL_ALIASES = {
    "IB": "IBKR",
    "IBKR": "IBKR",
    "盈透": "IBKR",
    "盈透证券": "IBKR",
}
PORTFOLIO_MUTATION_LOCK = RLock()


def utc_now_iso() -> str:
    return datetime.now().isoformat()


def ensure_data_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    OPERATIONS_DIR.mkdir(parents=True, exist_ok=True)
    PENDING_DIR.mkdir(parents=True, exist_ok=True)


def to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return default
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def normalize_account_name(value: Any) -> str:
    account = str(value or "").strip()
    if not account:
        return ""
    upper = account.upper()
    for alias, canonical in ACCOUNT_CANONICAL_ALIASES.items():
        if upper == alias.upper():
            return canonical
    return account


def strip_code_prefix(raw_code: Any) -> tuple[str, Optional[str], Optional[str]]:
    """Return user-friendly code plus inferred currency and asset type."""
    code = str(raw_code or "").strip()
    if not code:
        return "", None, None

    upper = code.upper()
    if ":" in upper:
        prefix, rest = upper.split(":", 1)
        if prefix in {"SHE", "SHA"}:
            return rest, "CNY", "stock"
        if prefix == "HKG":
            return rest.zfill(4), "HKD", "stock"
        if prefix in {"NASDAQ", "NYSE", "AMEX"}:
            return rest, "USD", "stock"
    return code.upper() if code.isascii() else code, None, None


def expected_currency_for_code(raw_code: Any) -> Optional[str]:
    code, inferred_currency, _ = strip_code_prefix(raw_code)
    if inferred_currency:
        return inferred_currency
    if code.isdigit() and len(code) == 4:
        return "HKD"
    if code.isdigit() and len(code) == 6:
        return "CNY"
    if code and code.isascii() and not code.isdigit():
        return "USD"
    return None


def infer_position_defaults(position: Dict[str, Any]) -> Dict[str, Any]:
    code = str(position.get("code") or "").strip()
    currency = str(position.get("currency") or "").upper()
    asset_type = str(position.get("asset_type") or "").strip()

    if not currency:
        if code.isdigit() and len(code) == 6:
            currency = "CNY"
        elif code.isdigit() and len(code) == 4:
            currency = "HKD"
        elif code.isascii() and code and not code.isdigit():
            currency = "USD"

    # This ledger treats every code-bearing tradable instrument uniformly as
    # a stock-like position. ETF/LOF/fund labels from older data are legacy
    # classifications only and do not affect valuation.
    if code:
        asset_type = "stock"
    elif asset_type in {"stock", "cash"}:
        asset_type = asset_type
    else:
        asset_type = "custom"

    position["currency"] = currency
    position["asset_type"] = asset_type
    return position


def position_identity(position: Dict[str, Any]) -> PositionIdentity:
    """Return the canonical identity: account + code + currency."""
    account = normalize_account_name(position.get("account") or position.get("group"))
    code, inferred_currency, _ = strip_code_prefix(position.get("code") or position.get("symbol"))
    currency = str(position.get("currency") or inferred_currency or "").upper().strip()
    return account, code, currency


class PortfolioWriteService:
    def __init__(self, portfolio_file: Path = PORTFOLIO_FILE):
        self.portfolio_file = Path(portfolio_file)
        ensure_data_dirs()

    def load_portfolio(self) -> Dict[str, Any]:
        if not self.portfolio_file.exists():
            return {"positions": [], "cash": 0.0, "cash_accounts": [], "updated_at": utc_now_iso()}

        try:
            with open(self.portfolio_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"portfolio账本损坏，已拒绝按空账本继续写入：{self.portfolio_file}"
            ) from exc
        except OSError as exc:
            raise RuntimeError(f"无法读取portfolio账本：{self.portfolio_file}") from exc
        if not isinstance(data, dict):
            raise RuntimeError("portfolio账本顶层必须是JSON对象")

        positions = [self.normalize_position(p, for_storage=True) for p in data.get("positions", [])]
        cash_accounts = []
        for raw in data.get("cash_accounts", []):
            if not isinstance(raw, dict):
                raise RuntimeError("cash_accounts中的记录必须是JSON对象")
            raw_amount = to_float(raw.get("amount"), None)
            if raw_amount is None:
                raise RuntimeError("cash_accounts存在无效amount")
            item = self.normalize_cash_account(raw)
            if not item.get("account") and not item.get("currency") and abs(item.get("amount", 0.0)) <= 1e-12:
                continue
            if not item.get("account") or not item.get("currency"):
                raise RuntimeError("cash_accounts存在缺少account或currency的记录")
            cash_accounts.append(item)

        self._validate_portfolio_records(positions, cash_accounts)

        return {
            "positions": positions,
            "cash": to_float(data.get("cash"), 0.0) or 0.0,
            "cash_accounts": cash_accounts,
            "updated_at": data.get("updated_at") or utc_now_iso(),
        }

    def _validate_portfolio_records(
        self,
        positions: List[Dict[str, Any]],
        cash_accounts: List[Dict[str, Any]],
    ) -> None:
        position_ids = [position_identity(position) for position in positions]
        duplicate_positions = sorted(identity for identity, count in Counter(position_ids).items() if count > 1)
        if duplicate_positions:
            raise RuntimeError(f"portfolio账本存在重复持仓身份：{duplicate_positions}")
        for position, identity in zip(positions, position_ids):
            if not all(identity):
                raise RuntimeError(f"portfolio账本存在不完整持仓身份：{identity}")
            if identity[2] not in SUPPORTED_CURRENCIES:
                raise RuntimeError(f"portfolio账本包含暂不支持的币种：{identity[2]}")
            expected_currency = expected_currency_for_code(identity[1])
            if expected_currency and identity[2] != expected_currency:
                raise RuntimeError(
                    f"portfolio账本代码与币种不匹配：{identity[1]}通常使用{expected_currency}，当前为{identity[2]}"
                )
            quantity = to_float(position.get("quantity"), None)
            cost_price = to_float(position.get("cost_price"), None)
            if quantity is None or quantity <= 0:
                raise RuntimeError(f"portfolio账本持仓数量无效：{identity}")
            if cost_price is None or cost_price < 0:
                raise RuntimeError(f"portfolio账本成本价无效：{identity}")

        cash_ids = [(item["account"], item["currency"]) for item in cash_accounts]
        duplicate_cash = sorted(identity for identity, count in Counter(cash_ids).items() if count > 1)
        if duplicate_cash:
            raise RuntimeError(f"portfolio账本存在重复现金账户：{duplicate_cash}")
        for item, identity in zip(cash_accounts, cash_ids):
            if not all(identity):
                raise RuntimeError(f"portfolio账本存在不完整现金账户：{identity}")
            if identity[1] not in SUPPORTED_CURRENCIES:
                raise RuntimeError(f"portfolio账本包含暂不支持的币种：{identity[1]}")
            amount = to_float(item.get("amount"), None)
            if amount is None or amount < 0:
                raise RuntimeError(f"portfolio账本现金余额无效：{identity}")

    def save_portfolio_atomic(
        self,
        portfolio: Dict[str, Any],
        *,
        preserve_updated_at: bool = False,
    ) -> None:
        ensure_data_dirs()
        positions = [self.normalize_position(p, for_storage=True) for p in portfolio.get("positions", [])]
        cash_accounts = []
        for raw in portfolio.get("cash_accounts", []):
            if not isinstance(raw, dict) or to_float(raw.get("amount"), None) is None:
                raise RuntimeError("cash_accounts存在无效记录")
            item = self.normalize_cash_account(raw)
            if not item.get("account") and not item.get("currency") and abs(item.get("amount", 0.0)) <= 1e-12:
                continue
            cash_accounts.append(item)
        self._validate_portfolio_records(positions, cash_accounts)
        raw_legacy_cash = portfolio.get("cash", 0.0)
        legacy_cash = to_float(raw_legacy_cash, None)
        if legacy_cash is None or legacy_cash < 0:
            raise RuntimeError("legacy cash必须是非负有限数字")
        data = {
            "positions": positions,
            "cash": legacy_cash,
            "cash_accounts": cash_accounts,
            "updated_at": (
                str(portfolio.get("updated_at") or utc_now_iso())
                if preserve_updated_at else utc_now_iso()
            ),
        }
        self.portfolio_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.portfolio_file.with_name(
            f".{self.portfolio_file.name}.{uuid4().hex}.tmp"
        )
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.portfolio_file)
        finally:
            tmp_path.unlink(missing_ok=True)

    def backup_portfolio(self) -> str:
        ensure_data_dirs()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup_path = BACKUPS_DIR / f"portfolio.json.bak-{stamp}-{uuid4().hex[:8]}"
        portfolio = self.load_portfolio()
        with open(backup_path, "w", encoding="utf-8") as f:
            json.dump(portfolio, f, ensure_ascii=False, indent=2)
            f.write("\n")
        return f"dashboard/data/backups/{backup_path.name}"

    def normalize_position(self, raw: Dict[str, Any], for_storage: bool = False) -> Dict[str, Any]:
        """
        Normalize legacy aliases into the canonical schema.

        Accepted input aliases:
        - group -> account
        - symbol -> code

        Returned/stored data never contains group, symbol, market, exchange or
        canonical_symbol.
        """
        raw_position = {
            k: v for k, v in dict(raw or {}).items()
            if k not in PROHIBITED_FIELDS
        }
        position: Dict[str, Any] = {}

        account = normalize_account_name(raw_position.get("account") or raw_position.get("group"))
        if account:
            position["account"] = account

        raw_code = raw_position.get("code") or raw_position.get("symbol")
        code, inferred_currency, inferred_asset_type = strip_code_prefix(raw_code)
        if code:
            position["code"] = code

        name = str(raw_position.get("name") or "").strip()
        if name:
            position["name"] = name

        explicit_currency = str(raw_position.get("currency") or "").upper().strip()
        if explicit_currency or inferred_currency:
            position["currency"] = explicit_currency or inferred_currency

        asset_type = str(raw_position.get("asset_type") or "").strip().lower()
        if code:
            # Canonical user-facing type: all listed/tradable code-bearing
            # instruments (stocks, ETF, LOF, listed funds) are stock.
            position["asset_type"] = "stock"
        elif asset_type in {"stock", "cash"}:
            position["asset_type"] = asset_type
        elif asset_type or inferred_asset_type:
            position["asset_type"] = "custom"

        quantity = to_float(raw_position.get("quantity"), None)
        cost_price = to_float(raw_position.get("cost_price"), None)
        total_cost = to_float(raw_position.get("total_cost"), None)
        fee = to_float(raw_position.get("fee"), None)

        if cost_price is None and quantity not in (None, 0) and total_cost is not None:
            cost_price = total_cost / quantity
        if total_cost is None and quantity is not None and cost_price is not None:
            total_cost = quantity * cost_price

        if quantity is not None:
            position["quantity"] = quantity
            position["available_qty"] = to_float(raw_position.get("available_qty"), quantity)
        if cost_price is not None:
            position["cost_price"] = cost_price
            position["current_price"] = to_float(raw_position.get("current_price"), cost_price)
        if total_cost is not None:
            position["total_cost"] = total_cost
        if fee is not None:
            position["fee"] = fee

        position["note"] = str(raw_position.get("note") or "")
        position["source"] = str(raw_position.get("source") or "manual")
        position["side"] = str(raw_position.get("side") or "long")
        position = infer_position_defaults(position)

        now = utc_now_iso()
        position["created_at"] = str(raw_position.get("created_at") or now)
        position["updated_at"] = str(raw_position.get("updated_at") or now)

        # Operational metadata may exist in pending changes, but is never stored.
        if not for_storage:
            if raw_position.get("action_type"):
                position["action_type"] = str(raw_position["action_type"])
            if raw_position.get("action"):
                position["action"] = str(raw_position["action"])
            if raw_position.get("amount") is not None:
                position["amount"] = to_float(raw_position.get("amount"), None)

        if for_storage:
            position = {k: v for k, v in position.items() if k in POSITION_STORAGE_FIELDS}

        return position

    def normalize_cash_account(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        account = normalize_account_name((raw or {}).get("account"))
        currency = str((raw or {}).get("currency") or "").upper().strip()
        amount = to_float((raw or {}).get("amount"), 0.0) or 0.0
        return {
            "account": account,
            "currency": currency,
            "amount": amount,
            "updated_at": str((raw or {}).get("updated_at") or utc_now_iso()),
        }

    def validate_confirmable_change(self, change: Dict[str, Any]) -> List[str]:
        action_type = str(change.get("action_type") or change.get("action") or "add_or_update")
        if action_type in {"deposit", "withdraw", "set_cash"}:
            missing = []
            if not str(change.get("account") or "").strip():
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
        normalized = self.normalize_position(change)
        missing = []
        if not normalized.get("account"):
            missing.append("account")
        if not normalized.get("code"):
            missing.append("code")
        currency = str(normalized.get("currency") or "").upper().strip()
        if not currency:
            missing.append("currency")
        elif currency not in SUPPORTED_CURRENCIES:
            missing.append("unsupported_currency")
        expected_currency = expected_currency_for_code(normalized.get("code"))
        if expected_currency and currency and currency != expected_currency:
            missing.append("currency_conflict")
        quantity = to_float(normalized.get("quantity"), None)
        if action_type != "delete":
            if quantity is None:
                missing.append("quantity")
            elif quantity <= 0:
                missing.append("positive_quantity")
        if action_type not in {"sell", "delete"}:
            cost_price = to_float(normalized.get("cost_price"), None)
            if cost_price is None:
                missing.append("cost_price")
            elif cost_price < 0:
                missing.append("cost_price_non_negative")
        return missing

    def apply_changes(self, portfolio: Dict[str, Any], changes: List[Dict[str, Any]]) -> tuple[Dict[str, Any], int]:
        result = deepcopy(portfolio)
        positions = [self.normalize_position(p, for_storage=True) for p in result.get("positions", [])]
        cash_accounts = [self.normalize_cash_account(item) for item in result.get("cash_accounts", [])]
        imported = 0

        for raw_change in changes:
            action_type = str(raw_change.get("action_type") or raw_change.get("action") or "add_or_update")

            if action_type in {"deposit", "withdraw", "set_cash"}:
                account = normalize_account_name(raw_change.get("account"))
                currency = str(raw_change.get("currency") or "").upper().strip()
                amount = to_float(raw_change.get("amount"), None)
                invalid_amount = (
                    amount is None
                    or (action_type in {"deposit", "withdraw"} and amount <= 0)
                    or (action_type == "set_cash" and amount < 0)
                )
                if not account or not currency or invalid_amount:
                    raise ValueError("现金操作缺少有效的account/currency/amount")
                matches = [item for item in cash_accounts if item["account"] == account and item["currency"] == currency]
                if len(matches) > 1:
                    raise ValueError(f"检测到重复现金账户: {(account, currency)}")
                current = matches[0]["amount"] if matches else 0.0
                if action_type == "deposit":
                    target = current + amount
                elif action_type == "withdraw":
                    target = current - amount
                    if target < -1e-8:
                        raise ValueError(f"{account}/{currency}现金不足，当前{current:g}，出金{amount:g}")
                else:
                    target = amount
                if matches:
                    matches[0]["amount"] = max(target, 0.0)
                    matches[0]["updated_at"] = utc_now_iso()
                else:
                    cash_accounts.append({
                        "account": account,
                        "currency": currency,
                        "amount": max(target, 0.0),
                        "updated_at": utc_now_iso(),
                    })
                imported += 1
                continue

            change = self.normalize_position(raw_change, for_storage=True)

            if action_type in {"sell", "delete"}:
                positions = self._apply_sell_or_delete(positions, change)
                imported += 1
                continue

            identity = position_identity(change)
            existing_matches = [p for p in positions if position_identity(p) == identity]
            if len(existing_matches) > 1:
                raise ValueError(f"检测到重复持仓身份: {identity}")
            existing = existing_matches[0] if existing_matches else None

            if action_type == "set_position":
                target_qty = to_float(change.get("quantity"), 0.0) or 0.0
                target_cost = to_float(change.get("cost_price"), 0.0) or 0.0
                fee = to_float(change.get("fee"), 0.0) or 0.0
                target_total = (target_qty * target_cost) + fee
                if target_qty <= 0:
                    raise ValueError("更新持仓数量必须大于0")
                if target_total < 0:
                    raise ValueError("更新持仓总成本不能小于0")
                if existing:
                    existing["quantity"] = target_qty
                    existing["available_qty"] = target_qty
                    existing["cost_price"] = target_total / target_qty
                    existing["total_cost"] = target_total
                    existing["name"] = change.get("name") or existing.get("name")
                    existing["asset_type"] = change.get("asset_type") or existing.get("asset_type")
                    existing["note"] = change.get("note", existing.get("note", ""))
                    existing["source"] = change.get("source") or existing.get("source") or "ai"
                    existing["updated_at"] = utc_now_iso()
                else:
                    change["quantity"] = target_qty
                    change["available_qty"] = target_qty
                    change["total_cost"] = target_total
                    change["cost_price"] = target_total / target_qty
                    positions.append(change)
                imported += 1
                continue

            if existing:
                old_qty = to_float(existing.get("quantity"), 0.0) or 0.0
                old_cost = to_float(existing.get("cost_price"), 0.0) or 0.0
                buy_qty = to_float(change.get("quantity"), 0.0) or 0.0
                buy_cost = to_float(change.get("cost_price"), 0.0) or 0.0
                fee = to_float(change.get("fee"), 0.0) or 0.0
                new_qty = old_qty + buy_qty
                if new_qty > 0:
                    existing["quantity"] = new_qty
                    existing["available_qty"] = new_qty
                    existing["cost_price"] = ((old_qty * old_cost) + (buy_qty * buy_cost) + fee) / new_qty
                    existing["total_cost"] = existing["quantity"] * existing["cost_price"]
                existing["name"] = change.get("name") or existing.get("name")
                existing["asset_type"] = change.get("asset_type") or existing.get("asset_type")
                existing["current_price"] = to_float(existing.get("current_price"), existing["cost_price"]) or existing["cost_price"]
                existing["note"] = change.get("note", existing.get("note", ""))
                existing["source"] = change.get("source") or existing.get("source") or "ai"
                existing["updated_at"] = utc_now_iso()
            else:
                buy_qty = to_float(change.get("quantity"), 0.0) or 0.0
                buy_cost = to_float(change.get("cost_price"), 0.0) or 0.0
                fee = to_float(change.get("fee"), 0.0) or 0.0
                if buy_qty > 0 and fee:
                    total_cost_with_fee = (buy_qty * buy_cost) + fee
                    if total_cost_with_fee < 0:
                        raise ValueError("手续费导致总成本小于0")
                    change["total_cost"] = total_cost_with_fee
                    change["cost_price"] = total_cost_with_fee / buy_qty
                positions.append(change)

            imported += 1

        result["positions"] = positions
        result["cash_accounts"] = [item for item in cash_accounts if abs(item.get("amount", 0.0)) > 1e-12]
        result["updated_at"] = utc_now_iso()
        return result, imported

    def _apply_sell_or_delete(self, positions: List[Dict[str, Any]], change: Dict[str, Any]) -> List[Dict[str, Any]]:
        identity = position_identity(change)
        qty = to_float(change.get("quantity"), None)
        kept: List[Dict[str, Any]] = []
        matched = False

        for position in positions:
            if position_identity(position) != identity:
                kept.append(position)
                continue

            matched = True
            if qty is None:
                continue

            current_qty = to_float(position.get("quantity"), 0.0) or 0.0
            if qty <= 0:
                raise ValueError("卖出数量必须大于0")
            if qty > current_qty:
                raise ValueError(
                    f"卖出数量{qty:g}超过现有持仓{current_qty:g}，暂不支持卖空"
                )

            new_qty = current_qty - qty
            if new_qty > 0:
                position["quantity"] = new_qty
                position["available_qty"] = min(
                    to_float(position.get("available_qty"), new_qty) or new_qty,
                    new_qty,
                )
                position["total_cost"] = new_qty * (to_float(position.get("cost_price"), 0.0) or 0.0)
                position["updated_at"] = utc_now_iso()
                kept.append(position)

        if not matched:
            raise FileNotFoundError(identity)
        return kept

    def create_operation(
        self,
        operation_type: str,
        summary: str,
        before_snapshot: Dict[str, Any],
        after_snapshot: Dict[str, Any],
        pending_action: Optional[Dict[str, Any]] = None,
        backup_path: str = "",
        imported_positions: int = 0,
    ) -> Dict[str, Any]:
        ensure_data_dirs()
        operation_id = str(uuid4())
        operation = {
            "operation_id": operation_id,
            "created_at": utc_now_iso(),
            "type": operation_type,
            "summary": summary,
            "imported_positions": imported_positions,
            "before_snapshot": before_snapshot,
            "after_snapshot": after_snapshot,
            "pending_action": pending_action or {},
            "backup_path": backup_path,
            "status": "confirmed" if operation_type != "rollback" else "rolled_back",
        }
        path = OPERATIONS_DIR / f"{operation_id}.json"
        tmp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(operation, f, ensure_ascii=False, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        finally:
            tmp_path.unlink(missing_ok=True)
        return operation

    def _commit_portfolio_change(
        self,
        *,
        before: Dict[str, Any],
        after: Dict[str, Any],
        operation_type: str,
        summary: str,
        pending_action: Optional[Dict[str, Any]] = None,
        imported_positions: int = 0,
    ) -> tuple[Dict[str, Any], str]:
        """Commit ledger and operation log as one recoverable unit.

        If operation-log creation fails after the atomic ledger replace, restore
        the exact before snapshot so no untracked write remains.
        """
        backup_path = self.backup_portfolio()
        self.save_portfolio_atomic(after)
        try:
            operation = self.create_operation(
                operation_type=operation_type,
                summary=summary,
                before_snapshot=before,
                after_snapshot=after,
                pending_action=pending_action,
                backup_path=backup_path,
                imported_positions=imported_positions,
            )
        except Exception:
            self.save_portfolio_atomic(before, preserve_updated_at=True)
            raise
        return operation, backup_path

    def _load_all_operations(self) -> List[Dict[str, Any]]:
        ensure_data_dirs()
        loaded: List[tuple[Dict[str, Any], int]] = []
        for path in OPERATIONS_DIR.glob("*.json"):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    operation = json.load(f)
                mtime_ns = path.stat().st_mtime_ns
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"operation日志损坏：{path.name}") from exc
            if not isinstance(operation, dict) or not operation.get("operation_id"):
                raise RuntimeError(f"operation日志格式错误：{path.name}")
            loaded.append((operation, mtime_ns))

        # Some filesystems assign the same mtime to several operation files
        # created in one atomic batch. created_at carries microseconds and is
        # therefore the primary ordering key; mtime/id are deterministic
        # fallbacks for legacy logs.
        loaded.sort(
            key=lambda entry: (
                str(entry[0].get("created_at") or ""),
                entry[1],
                str(entry[0].get("operation_id") or ""),
            ),
            reverse=True,
        )
        return [operation for operation, _mtime_ns in loaded]

    def _rolled_back_operation_ids(self, operations: Optional[List[Dict[str, Any]]] = None) -> set[str]:
        all_operations = operations if operations is not None else self._load_all_operations()
        return {
            str(operation.get("pending_action", {}).get("rolled_back_operation_id"))
            for operation in all_operations
            if operation.get("type") == "rollback"
            and operation.get("pending_action", {}).get("rolled_back_operation_id")
        }

    def list_operations(self, limit: int = 30) -> List[Dict[str, Any]]:
        operations = self._load_all_operations()
        rolled_back_ids = self._rolled_back_operation_ids(operations)
        summaries = []
        for operation in operations:
            operation_id = str(operation.get("operation_id") or "")
            operation_type = str(operation.get("type") or "")
            can_rollback = (
                operation_type != "rollback"
                and bool(operation.get("before_snapshot"))
                and operation_id not in rolled_back_ids
            )
            summaries.append({
                "operation_id": operation_id,
                "created_at": operation.get("created_at"),
                "type": operation_type,
                "summary": operation.get("summary"),
                "imported_positions": operation.get("imported_positions", 0),
                "can_rollback": can_rollback,
                "status": "rolled_back" if operation_id in rolled_back_ids else operation.get("status", "confirmed"),
            })
        return summaries[:limit]

    def find_confirm_operations_by_pending_id(self, pending_id: str) -> List[Dict[str, Any]]:
        target = str(pending_id or "").strip()
        if not target:
            return []
        operations = self._load_all_operations()
        rolled_back_ids = self._rolled_back_operation_ids(operations)
        matches: List[Dict[str, Any]] = []
        for operation in operations:
            pending_action = operation.get("pending_action", {}) or {}
            if operation.get("type") != "ai_confirm":
                continue
            if str(pending_action.get("pending_id") or "").strip() != target:
                continue
            result = dict(operation)
            result["is_rolled_back"] = str(result.get("operation_id") or "") in rolled_back_ids
            matches.append(result)
        return matches

    def find_confirm_operation_by_pending_id(self, pending_id: str) -> Optional[Dict[str, Any]]:
        """Return a legacy whole-card confirm operation.

        Item-level operations intentionally share a pending_id and are looked up
        through find_confirm_operation_by_pending_item instead.
        """
        matches = [
            operation for operation in self.find_confirm_operations_by_pending_id(pending_id)
            if not str((operation.get("pending_action") or {}).get("item_id") or "").strip()
        ]
        if len(matches) > 1:
            operation_ids = [str(operation.get("operation_id") or "") for operation in matches]
            raise RuntimeError(f"同一pending_id存在重复确认operation：{pending_id} -> {operation_ids}")
        return matches[0] if matches else None

    def find_confirm_operation_by_pending_item(
        self,
        pending_id: str,
        item_id: str,
    ) -> Optional[Dict[str, Any]]:
        target_item = str(item_id or "").strip()
        if not target_item:
            return None
        matches = [
            operation for operation in self.find_confirm_operations_by_pending_id(pending_id)
            if str((operation.get("pending_action") or {}).get("item_id") or "").strip() == target_item
        ]
        if len(matches) > 1:
            operation_ids = [str(operation.get("operation_id") or "") for operation in matches]
            raise RuntimeError(
                f"同一pending/item存在重复确认operation：{pending_id}/{target_item} -> {operation_ids}"
            )
        return matches[0] if matches else None


    def get_latest_rollbackable_operation(self) -> Dict[str, Any]:
        operations = self._load_all_operations()
        rolled_back_ids = self._rolled_back_operation_ids(operations)
        for operation in operations:
            operation_id = str(operation.get("operation_id") or "")
            if (
                operation.get("type") != "rollback"
                and operation.get("before_snapshot")
                and operation_id not in rolled_back_ids
            ):
                return operation
        raise FileNotFoundError("没有可撤回的写入操作")

    def load_operation(self, operation_id: str) -> Dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9-]{8,80}", str(operation_id or "")):
            raise FileNotFoundError(operation_id)
        path = OPERATIONS_DIR / f"{operation_id}.json"
        if not path.exists():
            raise FileNotFoundError(operation_id)
        try:
            with open(path, "r", encoding="utf-8") as f:
                operation = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"operation日志损坏：{path.name}") from exc
        if not isinstance(operation, dict):
            raise RuntimeError(f"operation日志格式错误：{path.name}")
        return operation

    def _semantic_changes(self, operation: Dict[str, Any]) -> List[Dict[str, Any]]:
        pending_action = operation.get("pending_action") or {}
        changes = pending_action.get("changes")
        if not isinstance(changes, list):
            return []
        return [dict(change) for change in changes if isinstance(change, dict)]

    def _snapshot_position_map(self, snapshot: Dict[str, Any]) -> Dict[PositionIdentity, Dict[str, Any]]:
        return {
            position_identity(position): self.normalize_position(position, for_storage=True)
            for position in snapshot.get("positions", [])
        }

    def _snapshot_cash_map(self, snapshot: Dict[str, Any]) -> Dict[tuple[str, str], float]:
        return {
            (
                str(item.get("account") or "").strip(),
                str(item.get("currency") or "").upper().strip(),
            ): to_float(item.get("amount"), 0.0) or 0.0
            for item in snapshot.get("cash_accounts", [])
            if str(item.get("account") or "").strip()
            and str(item.get("currency") or "").strip()
        }

    def _change_identity(
        self,
        change: Dict[str, Any],
    ) -> tuple[str, tuple[str, ...]]:
        action_type = str(change.get("action_type") or change.get("action") or "add_or_update")
        if action_type in {"deposit", "withdraw", "set_cash"}:
            return (
                "cash",
                (
                    str(change.get("account") or "").strip(),
                    str(change.get("currency") or "").upper().strip(),
                ),
            )
        return ("position", position_identity(change))

    def _affected_identities(
        self,
        operation: Dict[str, Any],
    ) -> tuple[set[PositionIdentity], set[tuple[str, str]]]:
        position_ids: set[PositionIdentity] = set()
        cash_ids: set[tuple[str, str]] = set()
        for change in self._semantic_changes(operation):
            kind, identity = self._change_identity(change)
            if kind == "cash":
                account, currency = identity
                if account and currency:
                    cash_ids.add((account, currency))
            else:
                account, code, currency = identity
                if account and code and currency:
                    position_ids.add((account, code, currency))

        before = operation.get("before_snapshot") or {}
        after = operation.get("after_snapshot") or {}
        before_positions = self._snapshot_position_map(before)
        after_positions = self._snapshot_position_map(after)
        for identity in set(before_positions) | set(after_positions):
            if before_positions.get(identity) != after_positions.get(identity):
                position_ids.add(identity)
        before_cash = self._snapshot_cash_map(before)
        after_cash = self._snapshot_cash_map(after)
        for identity in set(before_cash) | set(after_cash):
            if abs(before_cash.get(identity, 0.0) - after_cash.get(identity, 0.0)) > 1e-8:
                cash_ids.add(identity)
        return position_ids, cash_ids

    def _operation_touches_identities(
        self,
        operation: Dict[str, Any],
        position_ids: set[PositionIdentity],
        cash_ids: set[tuple[str, str]],
    ) -> bool:
        op_positions, op_cash = self._affected_identities(operation)
        return bool(op_positions & position_ids or op_cash & cash_ids)

    def _replace_affected_from_snapshot(
        self,
        state: Dict[str, Any],
        snapshot: Dict[str, Any],
        position_ids: set[PositionIdentity],
        cash_ids: set[tuple[str, str]],
    ) -> Dict[str, Any]:
        result = deepcopy(state)
        state_positions = self._snapshot_position_map(result)
        snapshot_positions = self._snapshot_position_map(snapshot)
        for identity in position_ids:
            if identity in snapshot_positions:
                state_positions[identity] = snapshot_positions[identity]
            else:
                state_positions.pop(identity, None)
        result["positions"] = list(state_positions.values())

        state_cash = self._snapshot_cash_map(result)
        snapshot_cash = self._snapshot_cash_map(snapshot)
        for identity in cash_ids:
            if identity in snapshot_cash:
                state_cash[identity] = snapshot_cash[identity]
            else:
                state_cash.pop(identity, None)
        result["cash_accounts"] = [
            {
                "account": account,
                "currency": currency,
                "amount": amount,
                "updated_at": utc_now_iso(),
            }
            for (account, currency), amount in state_cash.items()
        ]
        return result

    def _can_apply_delta_inverse_without_absolute_conflict(
        self,
        operation: Dict[str, Any],
    ) -> bool:
        """Return whether inverse deltas are safe for this rollback.

        Incremental buys/sells/deposits/withdrawals compose additively. They can
        therefore be removed from the current ledger without replaying old
        history, unless a later active absolute set_position/set_cash touches
        the same identity. This avoids resurrecting stale historical writes
        while preserving the existing replay semantics for absolute updates.
        """
        incremental_actions = {"add_or_update", "sell", "deposit", "withdraw"}
        target_changes = self._semantic_changes(operation)
        if not target_changes:
            return False
        if any(
            str(change.get("action_type") or change.get("action") or "add_or_update")
            not in incremental_actions
            for change in target_changes
        ):
            return False

        target_id = str(operation.get("operation_id") or "")
        position_ids, cash_ids = self._affected_identities(operation)
        operations_desc = self._load_all_operations()
        rolled_back_ids = self._rolled_back_operation_ids(operations_desc)
        target_index = next(
            (
                index for index, candidate in enumerate(operations_desc)
                if str(candidate.get("operation_id") or "") == target_id
            ),
            None,
        )
        if target_index is None:
            return False

        for later in operations_desc[:target_index]:
            later_id = str(later.get("operation_id") or "")
            if later.get("type") == "rollback" or later_id in rolled_back_ids:
                continue
            if not self._operation_touches_identities(later, position_ids, cash_ids):
                continue
            later_changes = self._semantic_changes(later)
            if not later_changes:
                return False
            for change in later_changes:
                kind, identity = self._change_identity(change)
                relevant = (
                    kind == "cash" and tuple(identity) in cash_ids
                ) or (
                    kind == "position" and tuple(identity) in position_ids
                )
                if not relevant:
                    continue
                action_type = str(
                    change.get("action_type") or change.get("action") or "add_or_update"
                )
                if action_type in {"set_position", "set_cash"}:
                    return False
        return True

    def _replay_without_operation(
        self,
        current: Dict[str, Any],
        target: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Rebuild identities touched by one operation with that operation removed.

        Delta inversion is wrong when either the removed operation or a later
        operation is absolute (set_position/set_cash). New operation logs carry
        semantic changes, allowing the affected identities to be replayed in
        chronological order while preserving unrelated current data.
        """
        target_id = str(target.get("operation_id") or "")
        position_ids, cash_ids = self._affected_identities(target)
        if not target_id or not position_ids and not cash_ids:
            return None

        operations_desc = self._load_all_operations()
        operations = list(reversed(operations_desc))
        index_by_id = {
            str(operation.get("operation_id") or ""): index
            for index, operation in enumerate(operations)
        }
        if target_id not in index_by_id:
            return None
        target_index = index_by_id[target_id]
        rolled_back_ids = self._rolled_back_operation_ids(operations_desc)

        relevant_indexes = [
            index
            for index, operation in enumerate(operations)
            if operation.get("type") != "rollback"
            and self._operation_touches_identities(operation, position_ids, cash_ids)
        ]
        if not relevant_indexes:
            return None
        first_index = min(relevant_indexes)
        first_before = operations[first_index].get("before_snapshot")
        if not isinstance(first_before, dict):
            return None
        replay = deepcopy(first_before)

        for index in range(first_index, len(operations)):
            operation = operations[index]
            operation_id = str(operation.get("operation_id") or "")
            if operation.get("type") == "rollback":
                continue
            if not self._operation_touches_identities(operation, position_ids, cash_ids):
                continue
            if operation_id == target_id or operation_id in rolled_back_ids:
                continue

            changes = self._semantic_changes(operation)
            relevant_changes: List[Dict[str, Any]] = []
            for change in changes:
                kind, identity = self._change_identity(change)
                if kind == "cash" and tuple(identity) in cash_ids:
                    relevant_changes.append(change)
                elif kind == "position" and tuple(identity) in position_ids:
                    relevant_changes.append(change)

            if relevant_changes:
                replay, _ = self.apply_changes(replay, relevant_changes)
                continue

            # Historical logs created before semantic operation metadata can be
            # replayed exactly only when they occur before the removed action.
            # A later unknown transformation is safer to leave to legacy delta
            # inversion than to guess whether it was incremental or absolute.
            if index > target_index:
                return None
            after_snapshot = operation.get("after_snapshot")
            if not isinstance(after_snapshot, dict):
                return None
            replay = self._replace_affected_from_snapshot(
                replay,
                after_snapshot,
                position_ids,
                cash_ids,
            )

        result = deepcopy(current)
        current_positions = self._snapshot_position_map(result)
        replay_positions = self._snapshot_position_map(replay)
        for identity in position_ids:
            if identity not in replay_positions:
                current_positions.pop(identity, None)
                continue
            restored = dict(replay_positions[identity])
            current_position = current_positions.get(identity)
            if current_position and to_float(current_position.get("current_price"), None) is not None:
                restored["current_price"] = current_position.get("current_price")
            restored["updated_at"] = utc_now_iso()
            current_positions[identity] = self.normalize_position(restored, for_storage=True)
        result["positions"] = list(current_positions.values())

        current_cash = self._snapshot_cash_map(result)
        replay_cash = self._snapshot_cash_map(replay)
        for identity in cash_ids:
            if identity in replay_cash:
                current_cash[identity] = replay_cash[identity]
            else:
                current_cash.pop(identity, None)
        result["cash_accounts"] = [
            {
                "account": account,
                "currency": currency,
                "amount": amount,
                "updated_at": utc_now_iso(),
            }
            for (account, currency), amount in current_cash.items()
        ]
        result["updated_at"] = utc_now_iso()
        return result

    def _apply_selective_inverse(
        self,
        current: Dict[str, Any],
        operation: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Undo one historical operation without discarding later unrelated writes.

        The operation's before/after snapshots define a quantity and cost-basis
        delta per canonical position identity. The inverse delta is applied to
        the current portfolio. This preserves positions created or changed by
        later operations on other identities.
        """
        before = operation.get("before_snapshot") or {}
        after = operation.get("after_snapshot") or {}

        def mapped(snapshot: Dict[str, Any]) -> Dict[PositionIdentity, Dict[str, Any]]:
            return {
                position_identity(position): self.normalize_position(position, for_storage=True)
                for position in snapshot.get("positions", [])
            }

        before_map = mapped(before)
        after_map = mapped(after)
        current_map = mapped(current)
        affected = set(before_map) | set(after_map)
        epsilon = 1e-8

        for identity in affected:
            before_position = before_map.get(identity)
            after_position = after_map.get(identity)
            if before_position == after_position:
                continue

            current_position = current_map.get(identity)
            before_qty = to_float((before_position or {}).get("quantity"), 0.0) or 0.0
            after_qty = to_float((after_position or {}).get("quantity"), 0.0) or 0.0
            current_qty = to_float((current_position or {}).get("quantity"), 0.0) or 0.0

            before_total = to_float((before_position or {}).get("total_cost"), None)
            if before_total is None:
                before_total = before_qty * (to_float((before_position or {}).get("cost_price"), 0.0) or 0.0)
            after_total = to_float((after_position or {}).get("total_cost"), None)
            if after_total is None:
                after_total = after_qty * (to_float((after_position or {}).get("cost_price"), 0.0) or 0.0)
            current_total = to_float((current_position or {}).get("total_cost"), None)
            if current_total is None:
                current_total = current_qty * (to_float((current_position or {}).get("cost_price"), 0.0) or 0.0)

            quantity_delta = after_qty - before_qty
            total_cost_delta = after_total - before_total
            target_qty = current_qty - quantity_delta
            target_total = current_total - total_cost_delta

            if target_qty < -epsilon:
                raise ValueError(
                    f"无法单独撤回{identity}：后续操作已消耗该持仓，请先撤回相关的后续操作"
                )
            if target_total < -epsilon:
                raise ValueError(
                    f"无法单独撤回{identity}：当前成本基础与历史操作冲突"
                )

            if abs(target_qty) <= epsilon:
                current_map.pop(identity, None)
                continue

            template = dict(current_position or before_position or after_position or {})
            template["account"], template["code"], template["currency"] = identity
            template["quantity"] = target_qty
            template["available_qty"] = min(
                to_float(template.get("available_qty"), target_qty) or target_qty,
                target_qty,
            )
            template["total_cost"] = max(target_total, 0.0)
            template["cost_price"] = template["total_cost"] / target_qty
            template["updated_at"] = utc_now_iso()

            # When no later version exists, restore descriptive metadata from
            # the state before the operation.
            if current_position is None and before_position:
                for key in ("name", "asset_type", "note", "source", "side", "current_price", "created_at"):
                    if key in before_position:
                        template[key] = before_position[key]
            current_map[identity] = self.normalize_position(template, for_storage=True)

        def cash_map(snapshot: Dict[str, Any]) -> Dict[tuple[str, str], float]:
            return {
                (str(item.get("account") or "").strip(), str(item.get("currency") or "").upper().strip()):
                to_float(item.get("amount"), 0.0) or 0.0
                for item in snapshot.get("cash_accounts", [])
                if str(item.get("account") or "").strip() and str(item.get("currency") or "").strip()
            }

        before_cash_accounts = cash_map(before)
        after_cash_accounts = cash_map(after)
        current_cash_accounts = cash_map(current)
        for identity in set(before_cash_accounts) | set(after_cash_accounts):
            delta = after_cash_accounts.get(identity, 0.0) - before_cash_accounts.get(identity, 0.0)
            target = current_cash_accounts.get(identity, 0.0) - delta
            if target < -epsilon:
                raise ValueError(f"无法单独撤回现金操作{identity}：后续操作已使用相关现金")
            if abs(target) <= epsilon:
                current_cash_accounts.pop(identity, None)
            else:
                current_cash_accounts[identity] = target

        current_cash = to_float(current.get("cash"), 0.0) or 0.0
        before_cash = to_float(before.get("cash"), 0.0) or 0.0
        after_cash = to_float(after.get("cash"), 0.0) or 0.0
        target_cash = current_cash - (after_cash - before_cash)
        if target_cash < -epsilon:
            raise ValueError("无法单独撤回：后续操作已使用相关现金")

        result = deepcopy(current)
        result["positions"] = list(current_map.values())
        result["cash_accounts"] = [
            {"account": account, "currency": currency, "amount": amount, "updated_at": utc_now_iso()}
            for (account, currency), amount in current_cash_accounts.items()
        ]
        result["cash"] = 0.0 if abs(target_cash) <= epsilon else target_cash
        result["updated_at"] = utc_now_iso()
        return result

    def rollback_operation(self, operation_id: str) -> Dict[str, Any]:
        with PORTFOLIO_MUTATION_LOCK:
            operation = self.load_operation(operation_id)
            if operation.get("type") == "rollback":
                raise ValueError("回滚操作本身不能再次回滚")
            if operation_id in self._rolled_back_operation_ids():
                raise ValueError("该操作已经撤回")
            before_snapshot = operation.get("before_snapshot")
            if not before_snapshot:
                raise ValueError("operation does not have before_snapshot")
            current = self.load_portfolio()
            if self._can_apply_delta_inverse_without_absolute_conflict(operation):
                restored = self._apply_selective_inverse(current, operation)
            else:
                restored = self._replay_without_operation(current, operation)
                if restored is None:
                    restored = self._apply_selective_inverse(current, operation)
            rollback, backup_path = self._commit_portfolio_change(
                before=current,
                after=restored,
                operation_type="rollback",
                summary=f"Rollback operation {operation_id}",
                pending_action={"rolled_back_operation_id": operation_id},
                imported_positions=0,
            )

            pending_id = str(operation.get("pending_action", {}).get("pending_id") or "").strip()
            if pending_id and re.fullmatch(r"[A-Za-z0-9-]{8,80}", pending_id):
                pending_path = PENDING_DIR / f"{pending_id}.json"
                if pending_path.exists():
                    try:
                        with open(pending_path, "r", encoding="utf-8") as f:
                            pending = json.load(f)
                        item_id = str(operation.get("pending_action", {}).get("item_id") or "").strip()
                        if item_id and isinstance(pending.get("items"), list):
                            matched_item = None
                            for item in pending["items"]:
                                if str(item.get("item_id") or "") == item_id:
                                    matched_item = item
                                    break
                            if matched_item is not None:
                                matched_item["status"] = "rolled_back"
                                matched_item["requires_confirmation"] = False
                                matched_item["rolled_back_at"] = utc_now_iso()
                                matched_item["rollback_operation_id"] = rollback["operation_id"]
                                statuses = [str(item.get("status") or "pending") for item in pending["items"]]
                                pending["pending_item_count"] = sum(value == "pending" for value in statuses)
                                pending["confirmed_item_count"] = sum(value == "confirmed" for value in statuses)
                                pending["rolled_back_item_count"] = sum(value == "rolled_back" for value in statuses)
                                if statuses and all(value == "rolled_back" for value in statuses):
                                    pending["status"] = "rolled_back"
                                elif any(value == "pending" for value in statuses):
                                    pending["status"] = "partially_confirmed"
                                else:
                                    pending["status"] = "partially_rolled_back"
                                open_items = [item for item in pending["items"] if item.get("status") == "pending"]
                                pending["can_confirm_all"] = bool(open_items) and all(
                                    not item.get("missing_fields") and not item.get("revision_options")
                                    for item in open_items
                                )
                                pending["requires_confirmation"] = pending["can_confirm_all"]
                        else:
                            pending["status"] = "rolled_back"
                            pending["requires_confirmation"] = False
                            pending["rolled_back_at"] = utc_now_iso()
                            pending["rollback_operation_id"] = rollback["operation_id"]
                        tmp_path = pending_path.with_name(f".{pending_path.name}.{uuid4().hex}.tmp")
                        try:
                            with open(tmp_path, "w", encoding="utf-8") as f:
                                json.dump(pending, f, ensure_ascii=False, indent=2)
                                f.write("\n")
                                f.flush()
                                os.fsync(f.fileno())
                            os.replace(tmp_path, pending_path)
                        finally:
                            tmp_path.unlink(missing_ok=True)
                    except Exception as exc:
                        print(f"[Rollback] 更新pending状态失败: {exc}")

            return {
                "ok": True,
                "rolled_back_operation_id": operation_id,
                "rollback_operation_id": rollback["operation_id"],
                "backup_path": backup_path,
            }


    def rollback_latest_operation(self) -> Dict[str, Any]:
        with PORTFOLIO_MUTATION_LOCK:
            operation = self.get_latest_rollbackable_operation()
            result = self.rollback_operation(str(operation["operation_id"]))
            result["summary"] = operation.get("summary") or operation.get("type") or "最近一次写入"
            result["operation_type"] = operation.get("type")
            pending_action = operation.get("pending_action") or {}
            changes = pending_action.get("changes") or []
            if changes:
                change = changes[0]
                account = str(change.get("account") or "").strip()
                target = str(change.get("name") or change.get("code") or change.get("currency") or "记录").strip()
                quantity = change.get("quantity")
                amount = change.get("amount")
                description = "/".join(part for part in [account, target] if part)
                if quantity is not None:
                    description = f"{description}，数量{quantity:g}" if isinstance(quantity, (int, float)) else f"{description}，数量{quantity}"
                elif amount is not None:
                    description = f"{description}，金额{amount:g}" if isinstance(amount, (int, float)) else f"{description}，金额{amount}"
                result["description"] = description
            return result

    def safe_set_cash_account(self, account: str, currency: str, amount: float) -> Dict[str, Any]:
        change = {
            "action_type": "set_cash",
            "account": account,
            "currency": currency,
            "amount": amount,
            "source": "manual",
        }
        missing = self.validate_confirmable_change(change)
        if missing:
            raise ValueError(f"现金操作缺少: {', '.join(missing)}")
        return self.safe_add_positions(
            [change],
            summary=f"设置账户现金 {account}/{currency}={amount:g}",
        )

    def safe_add_positions(
        self,
        changes: List[Dict[str, Any]],
        summary: str,
        pending_action: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        with PORTFOLIO_MUTATION_LOCK:
            before = self.load_portfolio()
            for change in changes:
                missing = self.validate_confirmable_change(change)
                if missing:
                    action_type = str(change.get("action_type") or change.get("action") or "add_or_update")
                    if action_type in {"deposit", "withdraw", "set_cash"}:
                        raise ValueError("现金操作缺少有效的account/currency/amount")
                    raise ValueError(f"写入字段无效：{', '.join(missing)}")
            after, imported = self.apply_changes(before, changes)
            operation, backup_path = self._commit_portfolio_change(
                before=before,
                after=after,
                operation_type="manual_add" if not pending_action else "ai_confirm",
                summary=summary,
                pending_action=pending_action or {"changes": deepcopy(changes)},
                imported_positions=imported,
            )
            return {
                "ok": True,
                "operation_id": operation["operation_id"],
                "imported_positions": imported,
                "backup_path": backup_path,
                "portfolio_updated": True,
            }

    def safe_add_items_atomic(
        self,
        items: List[Dict[str, Any]],
        *,
        pending_id: str,
        summary: str,
    ) -> Dict[str, Any]:
        """Atomically apply multiple independently rollbackable child items.

        Portfolio data is replaced once. Each child receives its own operation
        log with sequential before/after snapshots, so it can later be rolled
        back without reverting unrelated siblings.
        """
        if not items:
            raise ValueError("没有可写入的子项")
        with PORTFOLIO_MUTATION_LOCK:
            before_all = self.load_portfolio()
            simulated = deepcopy(before_all)
            steps: List[Dict[str, Any]] = []
            imported_total = 0

            for index, item in enumerate(items):
                item_id = str(item.get("item_id") or "").strip()
                changes = [dict(change) for change in item.get("changes", [])]
                if not item_id or not changes:
                    raise ValueError(f"第{index + 1}个子项缺少item_id或changes")
                for change in changes:
                    missing = self.validate_confirmable_change(change)
                    if missing:
                        raise ValueError(f"第{index + 1}个子项字段无效：{', '.join(missing)}")
                step_before = deepcopy(simulated)
                simulated, imported = self.apply_changes(simulated, changes)
                imported_total += imported
                steps.append({
                    "item_id": item_id,
                    "changes": changes,
                    "before": step_before,
                    "after": deepcopy(simulated),
                    "summary": str(item.get("summary") or f"确认第{index + 1}条记录"),
                    "imported": imported,
                })

            backup_path = self.backup_portfolio()
            self.save_portfolio_atomic(simulated)
            created_operation_ids: List[str] = []
            operations: List[Dict[str, Any]] = []
            batch_id = str(uuid4())
            try:
                for step in steps:
                    operation = self.create_operation(
                        operation_type="ai_confirm",
                        summary=step["summary"],
                        before_snapshot=step["before"],
                        after_snapshot=step["after"],
                        pending_action={
                            "pending_id": pending_id,
                            "item_id": step["item_id"],
                            "batch_id": batch_id,
                            "changes": step["changes"],
                        },
                        backup_path=backup_path,
                        imported_positions=step["imported"],
                    )
                    created_operation_ids.append(str(operation["operation_id"]))
                    operations.append(operation)
            except Exception:
                self.save_portfolio_atomic(before_all, preserve_updated_at=True)
                for operation_id in created_operation_ids:
                    (OPERATIONS_DIR / f"{operation_id}.json").unlink(missing_ok=True)
                raise

            return {
                "ok": True,
                "batch_id": batch_id,
                "operation_ids": created_operation_ids,
                "item_operations": [
                    {
                        "item_id": step["item_id"],
                        "operation_id": operation["operation_id"],
                        "imported_positions": step["imported"],
                    }
                    for step, operation in zip(steps, operations)
                ],
                "imported_positions": imported_total,
                "backup_path": backup_path,
                "portfolio_updated": True,
            }

    def safe_update_prices(self, prices: Dict[PositionIdentity, float]) -> Dict[str, Any]:
        """Patch quote prices into the latest ledger without replacing cash/positions."""
        with PORTFOLIO_MUTATION_LOCK:
            portfolio = self.load_portfolio()
            updated = 0
            for position in portfolio.get("positions", []):
                identity = position_identity(position)
                if identity not in prices:
                    continue
                price = to_float(prices[identity], None)
                if price is None or price <= 0:
                    continue
                position["current_price"] = price
                position["updated_at"] = utc_now_iso()
                updated += 1
            if updated:
                self.save_portfolio_atomic(portfolio)
            return {"ok": True, "updated_prices": updated}

    def safe_update_position(
        self,
        account: str,
        code: str,
        currency: str,
        updates: Dict[str, Any],
    ) -> Dict[str, Any]:
        with PORTFOLIO_MUTATION_LOCK:
            identity = position_identity({"account": account, "code": code, "currency": currency})
            before = self.load_portfolio()
            matches = [p for p in before.get("positions", []) if position_identity(p) == identity]
            if not matches:
                raise FileNotFoundError(identity)
            if len(matches) > 1:
                raise ValueError(f"检测到重复持仓身份: {identity}")

            existing = matches[0]
            updated = dict(existing)
            if updates.get("quantity") is not None:
                quantity = to_float(updates.get("quantity"), None)
                if quantity is None or quantity <= 0:
                    raise ValueError("持仓数量必须大于0；清空持仓请使用删除")
                updated["quantity"] = quantity
                updated["available_qty"] = quantity
            if updates.get("cost_price") is not None:
                cost_price = to_float(updates.get("cost_price"), None)
                if cost_price is None or cost_price < 0:
                    raise ValueError("成本价必须是非负有限数字")
                updated["cost_price"] = cost_price
            updated["total_cost"] = (
                (to_float(updated.get("quantity"), 0.0) or 0.0)
                * (to_float(updated.get("cost_price"), 0.0) or 0.0)
            )
            updated["updated_at"] = utc_now_iso()
            updated = self.normalize_position(updated, for_storage=True)

            after = deepcopy(before)
            after["positions"] = [
                updated if position_identity(p) == identity else self.normalize_position(p, for_storage=True)
                for p in before.get("positions", [])
            ]
            operation, backup_path = self._commit_portfolio_change(
                before=before,
                after=after,
                operation_type="manual_update",
                summary=f"手动更新持仓 {account}/{code}/{currency}",
                pending_action={
                    "changes": [{
                        **deepcopy(updated),
                        "action_type": "set_position",
                    }],
                },
                imported_positions=1,
            )
            return {
                "ok": True,
                "operation_id": operation["operation_id"],
                "backup_path": backup_path,
                "portfolio_updated": True,
            }

    def safe_remove_position(self, account: str, code: str, currency: str) -> Dict[str, Any]:
        with PORTFOLIO_MUTATION_LOCK:
            identity = position_identity({"account": account, "code": code, "currency": currency})
            before = self.load_portfolio()
            matches = [p for p in before.get("positions", []) if position_identity(p) == identity]
            if not matches:
                raise FileNotFoundError(identity)
            if len(matches) > 1:
                raise ValueError(f"检测到重复持仓身份: {identity}")

            after = deepcopy(before)
            after["positions"] = [
                p for p in after.get("positions", [])
                if position_identity(p) != identity
            ]
            after["updated_at"] = utc_now_iso()
            operation, backup_path = self._commit_portfolio_change(
                before=before,
                after=after,
                operation_type="manual_delete",
                summary=f"删除持仓 {account}/{code}/{currency}",
                pending_action={
                    "changes": [{
                        "action_type": "delete",
                        "account": account,
                        "code": code,
                        "currency": currency,
                        "name": matches[0].get("name"),
                        "asset_type": matches[0].get("asset_type", "stock"),
                    }],
                },
                imported_positions=1,
            )
            return {
                "ok": True,
                "operation_id": operation["operation_id"],
                "removed_positions": 1,
                "backup_path": backup_path,
                "portfolio_updated": True,
            }
