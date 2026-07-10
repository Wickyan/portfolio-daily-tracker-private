"""
开发/测试工具 API。

这些接口只用于内网测试环境，依赖外层 Nginx Basic Auth 保护。
"""
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel


router = APIRouter()

ResetTarget = Literal["portfolio", "chat", "pending", "operations", "all"]

DATA_DIR = Path("data").resolve()
BACKUPS_DIR = DATA_DIR / "backups"


class ResetRequest(BaseModel):
    target: ResetTarget
    confirm_text: str


def _inside_data_dir(path: Path) -> bool:
    try:
        path.resolve().relative_to(DATA_DIR)
        return True
    except ValueError:
        return False


def _assert_safe_data_path(path: Path) -> Path:
    resolved = path.resolve()
    if not _inside_data_dir(resolved):
        raise HTTPException(status_code=400, detail="非法的数据路径")
    return resolved


def _create_backup_dir() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    backup_dir = BACKUPS_DIR / f"dev-reset-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    return backup_dir


def _backup_path(source: Path, backup_dir: Path) -> None:
    source = _assert_safe_data_path(source)
    if not source.exists():
        return

    relative_path = source.relative_to(DATA_DIR)
    destination = backup_dir / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)

    if source.is_dir():
        shutil.copytree(source, destination)
    else:
        shutil.copy2(source, destination)


def _clear_directory_contents(path: Path) -> None:
    path = _assert_safe_data_path(path)
    if path == BACKUPS_DIR.resolve():
        raise HTTPException(status_code=400, detail="禁止清空备份目录")

    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        child = _assert_safe_data_path(child)
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def _reset_portfolio_file() -> None:
    portfolio_path = _assert_safe_data_path(DATA_DIR / "portfolio.json")
    payload = {
        "positions": [],
        "cash": 0.0,
        "cash_accounts": [],
        "updated_at": datetime.now().isoformat(),
    }

    portfolio_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = portfolio_path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_path, portfolio_path)


def _reload_portfolio_provider() -> None:
    """让已运行的服务丢弃旧内存持仓，重新读取 reset 后的文件。"""
    try:
        from backend.main import get_agent_service

        service = get_agent_service()
        if service is not None:
            service._init_portfolio_provider()
    except Exception as exc:
        print(f"[DevReset] 持仓 provider 重载失败: {exc}")


def _reset_chat() -> None:
    _clear_directory_contents(DATA_DIR / "conversations")
    _clear_directory_contents(DATA_DIR / "conversation_images")


def _reset_pending() -> None:
    _clear_directory_contents(DATA_DIR / "pending_actions")


def _reset_operations() -> None:
    _clear_directory_contents(DATA_DIR / "operations")


def _targets_for(target: ResetTarget) -> list[str]:
    if target == "all":
        return ["portfolio", "chat", "pending"]
    return [target]


@router.post("/reset")
async def reset_dev_data(request: ResetRequest):
    """备份并重置指定测试数据。"""
    if request.confirm_text != "RESET":
        raise HTTPException(status_code=400, detail="请准确输入 RESET 确认重置")

    backup_dir = _create_backup_dir()
    actions = _targets_for(request.target)

    try:
        if "portfolio" in actions:
            _backup_path(DATA_DIR / "portfolio.json", backup_dir)
        if "chat" in actions:
            _backup_path(DATA_DIR / "conversations", backup_dir)
            _backup_path(DATA_DIR / "conversation_images", backup_dir)
        if "pending" in actions:
            _backup_path(DATA_DIR / "pending_actions", backup_dir)
        if "operations" in actions:
            _backup_path(DATA_DIR / "operations", backup_dir)

        if "portfolio" in actions:
            _reset_portfolio_file()
            _reload_portfolio_provider()
        if "chat" in actions:
            _reset_chat()
        if "pending" in actions:
            _reset_pending()
        if "operations" in actions:
            _reset_operations()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"重置失败: {exc}") from exc

    return {
        "ok": True,
        "message": "测试数据已备份并重置",
        "backup_dir": f"dashboard/data/backups/{backup_dir.name}",
    }


@router.get("/backups")
async def list_dev_reset_backups():
    """返回最近的开发重置备份目录。"""
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    backups = [
        {
            "name": path.name,
            "path": f"dashboard/data/backups/{path.name}",
            "created_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
        }
        for path in sorted(BACKUPS_DIR.glob("dev-reset-*"), reverse=True)
        if path.is_dir()
    ]
    return {"backups": backups[:20]}
