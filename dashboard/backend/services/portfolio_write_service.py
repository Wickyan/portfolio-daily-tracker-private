"""
Safe portfolio write service.

All user-visible portfolio writes should go through this service so every
change gets a backup and an operation log. Stored positions never include
market/exchange/canonical_symbol.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4


DATA_DIR = Path("data")
PORTFOLIO_FILE = DATA_DIR / "portfolio.json"
BACKUPS_DIR = DATA_DIR / "backups"
OPERATIONS_DIR = DATA_DIR / "operations"
PENDING_DIR = DATA_DIR / "pending_actions"

PROHIBITED_FIELDS = {"market", "exchange", "canonical_symbol"}


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
    currency = position.get("currency")
    asset_type = position.get("asset_type")

    if not currency:
        if code.isdigit() and len(code) == 6:
            currency = "CNY"
        elif code.isdigit() and len(code) == 4:
            currency = "HKD"
        elif code.isascii() and code.replace(".", "").isalnum():
            currency = "USD"

    if not asset_type:
        if code.isdigit() and len(code) == 6:
            asset_type = "fund" if code.startswith(("15", "16", "50", "51", "52", "56", "58")) else "stock"
        elif code.isdigit() and len(code) == 4:
            asset_type = "stock"
        elif code:
            asset_type = "stock"
        else:
            asset_type = "custom"

    position["currency"] = str(currency or "").upper()
    position["asset_type"] = asset_type or "custom"
    return position


class PortfolioWriteService:
    def __init__(self, portfolio_file: Path = PORTFOLIO_FILE):
        self.portfolio_file = Path(portfolio_file)
        ensure_data_dirs()

    def load_portfolio(self) -> Dict[str, Any]:
        if not self.portfolio_file.exists():
            return {"positions": [], "cash": 0.0, "updated_at": utc_now_iso()}

        try:
            with open(self.portfolio_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}

        positions = [self.normalize_position(p, for_storage=True) for p in data.get("positions", [])]
        return {
            "positions": positions,
            "cash": to_float(data.get("cash"), 0.0) or 0.0,
            "updated_at": data.get("updated_at") or utc_now_iso(),
        }

    def save_portfolio_atomic(self, portfolio: Dict[str, Any]) -> None:
        ensure_data_dirs()
        data = {
            "positions": [self.normalize_position(p, for_storage=True) for p in portfolio.get("positions", [])],
            "cash": to_float(portfolio.get("cash"), 0.0) or 0.0,
            "updated_at": utc_now_iso(),
        }
        tmp_path = self.portfolio_file.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_path, self.portfolio_file)

    def backup_portfolio(self) -> str:
        ensure_data_dirs()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = BACKUPS_DIR / f"portfolio.json.bak-{stamp}"
        portfolio = self.load_portfolio()
        with open(backup_path, "w", encoding="utf-8") as f:
            json.dump(portfolio, f, ensure_ascii=False, indent=2)
            f.write("\n")
        return f"dashboard/data/backups/{backup_path.name}"

    def normalize_position(self, raw: Dict[str, Any], for_storage: bool = False) -> Dict[str, Any]:
        position = {k: v for k, v in dict(raw or {}).items() if k not in PROHIBITED_FIELDS}

        raw_code = position.get("code") or position.get("symbol")
        code, inferred_currency, inferred_asset_type = strip_code_prefix(raw_code)
        if code:
            position["code"] = code
            position["symbol"] = code
        else:
            position.pop("code", None)
            position.pop("symbol", None)

        if not position.get("currency") and inferred_currency:
            position["currency"] = inferred_currency
        if not position.get("asset_type") and inferred_asset_type:
            position["asset_type"] = inferred_asset_type

        account = str(position.get("account") or position.get("group") or "").strip()
        if account:
            position["account"] = account
            position["group"] = account

        name = str(position.get("name") or "").strip()
        if name:
            position["name"] = name

        quantity = to_float(position.get("quantity"), None)
        cost_price = to_float(position.get("cost_price"), None)
        total_cost = to_float(position.get("total_cost"), None)
        fee = to_float(position.get("fee"), None)

        if cost_price is None and quantity and total_cost is not None:
            cost_price = total_cost / quantity
        if total_cost is None and quantity is not None and cost_price is not None:
            total_cost = quantity * cost_price

        if quantity is not None:
            position["quantity"] = quantity
            position["available_qty"] = to_float(position.get("available_qty"), quantity) or quantity
        if cost_price is not None:
            position["cost_price"] = cost_price
            position["current_price"] = to_float(position.get("current_price"), cost_price) or cost_price
        if total_cost is not None:
            position["total_cost"] = total_cost
        if fee is not None:
            position["fee"] = fee

        position["note"] = str(position.get("note") or "")
        position["source"] = str(position.get("source") or "manual")
        position = infer_position_defaults(position)

        now = utc_now_iso()
        if not position.get("created_at"):
            position["created_at"] = now
        position["updated_at"] = now

        if for_storage:
            position.pop("market", None)
            position.pop("exchange", None)
            position.pop("canonical_symbol", None)

        return position

    def validate_confirmable_change(self, change: Dict[str, Any]) -> List[str]:
        missing = []
        if not (change.get("account") or change.get("group")):
            missing.append("account/group")
        if not change.get("code"):
            missing.append("code")
        if not change.get("currency"):
            missing.append("currency")
        if to_float(change.get("quantity"), None) is None:
            missing.append("quantity")
        if to_float(change.get("cost_price"), None) is None:
            missing.append("cost_price")
        return missing

    def apply_changes(self, portfolio: Dict[str, Any], changes: List[Dict[str, Any]]) -> tuple[Dict[str, Any], int]:
        result = deepcopy(portfolio)
        positions = [self.normalize_position(p, for_storage=True) for p in result.get("positions", [])]
        imported = 0

        for raw_change in changes:
            change = self.normalize_position(raw_change, for_storage=True)
            action_type = str(raw_change.get("action_type") or raw_change.get("action") or "add_or_update")

            if action_type in {"sell", "delete"}:
                positions = self._apply_sell_or_delete(positions, change)
                imported += 1
                continue

            account = change.get("account") or change.get("group")
            code = change.get("code")
            currency = change.get("currency")
            existing = next(
                (
                    p for p in positions
                    if (p.get("account") or p.get("group")) == account
                    and p.get("code") == code
                    and p.get("currency") == currency
                ),
                None,
            )

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
        result["updated_at"] = utc_now_iso()
        return result, imported

    def _apply_sell_or_delete(self, positions: List[Dict[str, Any]], change: Dict[str, Any]) -> List[Dict[str, Any]]:
        account = change.get("account") or change.get("group")
        code = change.get("code")
        currency = change.get("currency")
        qty = to_float(change.get("quantity"), None)
        kept = []
        for position in positions:
            same = (
                (not account or (position.get("account") or position.get("group")) == account)
                and position.get("code") == code
                and (not currency or position.get("currency") == currency)
            )
            if not same:
                kept.append(position)
                continue
            if qty is None:
                continue
            new_qty = (to_float(position.get("quantity"), 0.0) or 0.0) - qty
            if new_qty > 0:
                position["quantity"] = new_qty
                position["available_qty"] = min(to_float(position.get("available_qty"), new_qty) or new_qty, new_qty)
                position["total_cost"] = new_qty * (to_float(position.get("cost_price"), 0.0) or 0.0)
                position["updated_at"] = utc_now_iso()
                kept.append(position)
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

    def list_operations(self, limit: int = 30) -> List[Dict[str, Any]]:
        ensure_data_dirs()
        operations = []
        for path in sorted(OPERATIONS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    operation = json.load(f)
            except Exception:
                continue
            operations.append({
                "operation_id": operation.get("operation_id"),
                "created_at": operation.get("created_at"),
                "type": operation.get("type"),
                "summary": operation.get("summary"),
                "imported_positions": operation.get("imported_positions", 0),
                "can_rollback": bool(operation.get("before_snapshot")),
            })
        return operations[:limit]

    def load_operation(self, operation_id: str) -> Dict[str, Any]:
        path = OPERATIONS_DIR / f"{operation_id}.json"
        if not path.exists():
            raise FileNotFoundError(operation_id)
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def rollback_operation(self, operation_id: str) -> Dict[str, Any]:
        operation = self.load_operation(operation_id)
        before_snapshot = operation.get("before_snapshot")
        if not before_snapshot:
            raise ValueError("operation does not have before_snapshot")
        current = self.load_portfolio()
        backup_path = self.backup_portfolio()
        self.save_portfolio_atomic(before_snapshot)
        rollback = self.create_operation(
            operation_type="rollback",
            summary=f"Rollback operation {operation_id}",
            before_snapshot=current,
            after_snapshot=before_snapshot,
            pending_action={"rolled_back_operation_id": operation_id},
            backup_path=backup_path,
            imported_positions=0,
        )
        return {
            "ok": True,
            "rolled_back_operation_id": operation_id,
            "rollback_operation_id": rollback["operation_id"],
            "backup_path": backup_path,
        }

    def safe_add_positions(self, changes: List[Dict[str, Any]], summary: str, pending_action: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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

    def safe_remove_position(self, code: str) -> Dict[str, Any]:
        before = self.load_portfolio()
        backup_path = self.backup_portfolio()
        after = deepcopy(before)
        before_len = len(after.get("positions", []))
        after["positions"] = [p for p in after.get("positions", []) if p.get("code") != code and p.get("symbol") != code]
        removed = before_len - len(after["positions"])
        after["updated_at"] = utc_now_iso()
        self.save_portfolio_atomic(after)
        operation = self.create_operation(
            operation_type="manual_delete",
            summary=f"删除持仓 {code}",
            before_snapshot=before,
            after_snapshot=after,
            backup_path=backup_path,
            imported_positions=removed,
        )
        return {
            "ok": True,
            "operation_id": operation["operation_id"],
            "removed_positions": removed,
            "backup_path": backup_path,
            "portfolio_updated": True,
        }
