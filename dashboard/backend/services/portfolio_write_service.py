"""
Canonical portfolio schema and safe write helpers.

Stored positions use one user-facing identity only:
    account + code + currency

Legacy input aliases (group/symbol) are accepted at boundaries, but are never
written back to portfolio.json. market/exchange/canonical_symbol are discarded.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
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
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


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
    account = str(position.get("account") or position.get("group") or "").strip()
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
        except Exception:
            data = {}

        positions = [self.normalize_position(p, for_storage=True) for p in data.get("positions", [])]
        cash_accounts = [
            self.normalize_cash_account(item)
            for item in data.get("cash_accounts", [])
            if self.normalize_cash_account(item).get("account")
        ]
        return {
            "positions": positions,
            "cash": to_float(data.get("cash"), 0.0) or 0.0,
            "cash_accounts": cash_accounts,
            "updated_at": data.get("updated_at") or utc_now_iso(),
        }

    def save_portfolio_atomic(self, portfolio: Dict[str, Any]) -> None:
        ensure_data_dirs()
        data = {
            "positions": [self.normalize_position(p, for_storage=True) for p in portfolio.get("positions", [])],
            "cash": to_float(portfolio.get("cash"), 0.0) or 0.0,
            "cash_accounts": [
                self.normalize_cash_account(item)
                for item in portfolio.get("cash_accounts", [])
                if self.normalize_cash_account(item).get("account")
            ],
            "updated_at": utc_now_iso(),
        }
        tmp_path = self.portfolio_file.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_path, self.portfolio_file)

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

        account = str(raw_position.get("account") or raw_position.get("group") or "").strip()
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
        account = str((raw or {}).get("account") or "").strip()
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
            if not str(change.get("currency") or "").strip():
                missing.append("currency")
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
        if not normalized.get("currency"):
            missing.append("currency")
        if to_float(normalized.get("quantity"), None) is None:
            missing.append("quantity")
        if to_float(normalized.get("cost_price"), None) is None:
            missing.append("cost_price")
        return missing

    def apply_changes(self, portfolio: Dict[str, Any], changes: List[Dict[str, Any]]) -> tuple[Dict[str, Any], int]:
        result = deepcopy(portfolio)
        positions = [self.normalize_position(p, for_storage=True) for p in result.get("positions", [])]
        cash_accounts = [self.normalize_cash_account(item) for item in result.get("cash_accounts", [])]
        imported = 0

        for raw_change in changes:
            action_type = str(raw_change.get("action_type") or raw_change.get("action") or "add_or_update")

            if action_type in {"deposit", "withdraw", "set_cash"}:
                account = str(raw_change.get("account") or "").strip()
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
        with open(path, "w", encoding="utf-8") as f:
            json.dump(operation, f, ensure_ascii=False, indent=2)
            f.write("\n")
        return operation

    def _load_all_operations(self) -> List[Dict[str, Any]]:
        ensure_data_dirs()
        operations: List[Dict[str, Any]] = []
        for path in sorted(OPERATIONS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    operations.append(json.load(f))
            except Exception:
                continue
        return operations

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
        path = OPERATIONS_DIR / f"{operation_id}.json"
        if not path.exists():
            raise FileNotFoundError(operation_id)
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

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
        operation = self.load_operation(operation_id)
        if operation.get("type") == "rollback":
            raise ValueError("回滚操作本身不能再次回滚")
        if operation_id in self._rolled_back_operation_ids():
            raise ValueError("该操作已经撤回")
        before_snapshot = operation.get("before_snapshot")
        if not before_snapshot:
            raise ValueError("operation does not have before_snapshot")
        current = self.load_portfolio()
        restored = self._apply_selective_inverse(current, operation)
        backup_path = self.backup_portfolio()
        self.save_portfolio_atomic(restored)
        rollback = self.create_operation(
            operation_type="rollback",
            summary=f"Rollback operation {operation_id}",
            before_snapshot=current,
            after_snapshot=restored,
            pending_action={"rolled_back_operation_id": operation_id},
            backup_path=backup_path,
            imported_positions=0,
        )

        pending_id = str(operation.get("pending_action", {}).get("pending_id") or "").strip()
        if pending_id:
            pending_path = PENDING_DIR / f"{pending_id}.json"
            if pending_path.exists():
                try:
                    with open(pending_path, "r", encoding="utf-8") as f:
                        pending = json.load(f)
                    pending["status"] = "rolled_back"
                    pending["requires_confirmation"] = False
                    pending["rolled_back_at"] = utc_now_iso()
                    pending["rollback_operation_id"] = rollback["operation_id"]
                    tmp_path = pending_path.with_suffix(".json.tmp")
                    with open(tmp_path, "w", encoding="utf-8") as f:
                        json.dump(pending, f, ensure_ascii=False, indent=2)
                        f.write("\n")
                    os.replace(tmp_path, pending_path)
                except Exception as exc:
                    print(f"[Rollback] 更新pending状态失败: {exc}")

        return {
            "ok": True,
            "rolled_back_operation_id": operation_id,
            "rollback_operation_id": rollback["operation_id"],
            "backup_path": backup_path,
        }


    def rollback_latest_operation(self) -> Dict[str, Any]:
        operation = self.get_latest_rollbackable_operation()
        result = self.rollback_operation(str(operation["operation_id"]))
        result["summary"] = operation.get("summary") or operation.get("type") or "最近一次写入"
        result["operation_type"] = operation.get("type")
        pending_action = operation.get("pending_action") or {}
        changes = pending_action.get("changes") or []
        if changes:
            change = changes[0]
            account = str(change.get("account") or "").strip()
            target = str(change.get("name") or change.get("code") or "持仓").strip()
            quantity = change.get("quantity")
            description = "/".join(part for part in [account, target] if part)
            if quantity is not None:
                description = f"{description}，数量{quantity:g}" if isinstance(quantity, (int, float)) else f"{description}，数量{quantity}"
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
        before = self.load_portfolio()
        backup_path = self.backup_portfolio()
        after, imported = self.apply_changes(before, changes)
        self.save_portfolio_atomic(after)
        operation = self.create_operation(
            operation_type="manual_add" if not pending_action else "ai_confirm",
            summary=summary,
            before_snapshot=before,
            after_snapshot=after,
            pending_action=pending_action,
            backup_path=backup_path,
            imported_positions=imported,
        )
        return {
            "ok": True,
            "operation_id": operation["operation_id"],
            "imported_positions": imported,
            "backup_path": backup_path,
            "portfolio_updated": True,
        }

    def safe_update_position(
        self,
        account: str,
        code: str,
        currency: str,
        updates: Dict[str, Any],
    ) -> Dict[str, Any]:
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
            updated["quantity"] = to_float(updates.get("quantity"), existing.get("quantity"))
            updated["available_qty"] = updated["quantity"]
        if updates.get("cost_price") is not None:
            updated["cost_price"] = to_float(updates.get("cost_price"), existing.get("cost_price"))
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
        backup_path = self.backup_portfolio()
        self.save_portfolio_atomic(after)
        operation = self.create_operation(
            operation_type="manual_update",
            summary=f"手动更新持仓 {account}/{code}/{currency}",
            before_snapshot=before,
            after_snapshot=after,
            backup_path=backup_path,
            imported_positions=1,
        )
        return {
            "ok": True,
            "operation_id": operation["operation_id"],
            "backup_path": backup_path,
            "portfolio_updated": True,
        }

    def safe_remove_position(self, account: str, code: str, currency: str) -> Dict[str, Any]:
        identity = position_identity({"account": account, "code": code, "currency": currency})
        before = self.load_portfolio()
        matches = [p for p in before.get("positions", []) if position_identity(p) == identity]
        if not matches:
            raise FileNotFoundError(identity)
        if len(matches) > 1:
            raise ValueError(f"检测到重复持仓身份: {identity}")

        backup_path = self.backup_portfolio()
        after = deepcopy(before)
        after["positions"] = [
            p for p in after.get("positions", [])
            if position_identity(p) != identity
        ]
        after["updated_at"] = utc_now_iso()
        self.save_portfolio_atomic(after)
        operation = self.create_operation(
            operation_type="manual_delete",
            summary=f"删除持仓 {account}/{code}/{currency}",
            before_snapshot=before,
            after_snapshot=after,
            backup_path=backup_path,
            imported_positions=1,
        )
        return {
            "ok": True,
            "operation_id": operation["operation_id"],
            "removed_positions": 1,
            "backup_path": backup_path,
            "portfolio_updated": True,
        }
