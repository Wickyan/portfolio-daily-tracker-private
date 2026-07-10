"""
持仓 API 路由
"""
from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()


class AddPositionRequest(BaseModel):
    """添加持仓请求"""
    account: Optional[str] = None
    group: Optional[str] = None
    code: Optional[str] = None
    symbol: Optional[str] = None
    name: str
    currency: Optional[str] = None
    asset_type: Optional[str] = None
    quantity: float
    cost_price: float
    total_cost: Optional[float] = None
    fee: Optional[float] = None
    note: str = ""
    source: str = "manual"


class UpdatePositionRequest(BaseModel):
    """更新持仓请求"""
    quantity: Optional[int] = None
    cost_price: Optional[float] = None


def get_service():
    """获取 Agent 服务实例"""
    from backend.main import get_agent_service
    service = get_agent_service()
    if service is None:
        raise HTTPException(status_code=503, detail="服务尚未初始化")
    return service


@router.get("")
async def get_portfolio():
    """获取持仓列表"""
    service = get_service()
    return await service.get_portfolio()


@router.get("/live")
async def get_live_portfolio():
    """刷新行情后获取持仓列表"""
    service = get_service()
    await service.refresh_portfolio()
    return await service.get_portfolio()


@router.post("/add")
async def add_position(request: AddPositionRequest):
    """添加持仓"""
    from backend.services.portfolio_write_service import PortfolioWriteService
    from backend.api.portfolio_ai import reload_agent_portfolio_provider

    write_service = PortfolioWriteService()
    change = {
        "account": request.account or request.group,
        "group": request.group or request.account,
        "code": request.code or request.symbol,
        "symbol": request.symbol or request.code,
        "name": request.name,
        "currency": request.currency,
        "asset_type": request.asset_type,
        "quantity": request.quantity,
        "cost_price": request.cost_price,
        "total_cost": request.total_cost,
        "fee": request.fee,
        "note": request.note,
        "source": request.source or "manual",
    }
    missing = write_service.validate_confirmable_change(write_service.normalize_position(change))
    if missing:
        raise HTTPException(status_code=400, detail=f"缺少字段: {', '.join(missing)}")
    result = write_service.safe_add_positions([change], summary=f"手动新增持仓: {request.name}")
    reload_agent_portfolio_provider()
    return {**result, "message": f"已添加持仓: {request.name}({request.code or request.symbol})"}


@router.put("/{symbol}")
async def update_position(symbol: str, request: UpdatePositionRequest):
    """更新持仓"""
    from backend.services.portfolio_write_service import PortfolioWriteService, to_float
    from backend.api.portfolio_ai import reload_agent_portfolio_provider

    write_service = PortfolioWriteService()
    before = write_service.load_portfolio()
    existing = next((p for p in before.get("positions", []) if p.get("code") == symbol or p.get("symbol") == symbol), None)
    if not existing:
        raise HTTPException(status_code=404, detail="持仓不存在")
    updated = existing.copy()
    if request.quantity is not None:
        updated["quantity"] = request.quantity
    if request.cost_price is not None:
        updated["cost_price"] = request.cost_price
    updated["total_cost"] = (to_float(updated.get("quantity"), 0.0) or 0.0) * (to_float(updated.get("cost_price"), 0.0) or 0.0)

    backup_path = write_service.backup_portfolio()
    after = before.copy()
    after["positions"] = [
        write_service.normalize_position(updated, for_storage=True)
        if (p.get("code") == symbol or p.get("symbol") == symbol) else p
        for p in before.get("positions", [])
    ]
    write_service.save_portfolio_atomic(after)
    operation = write_service.create_operation(
        operation_type="manual_update",
        summary=f"手动更新持仓 {symbol}",
        before_snapshot=before,
        after_snapshot=after,
        backup_path=backup_path,
        imported_positions=1,
    )
    reload_agent_portfolio_provider()
    return {"ok": True, "operation_id": operation["operation_id"], "message": f"已更新持仓: {symbol}", "backup_path": backup_path}


@router.delete("/{symbol}")
async def remove_position(symbol: str):
    """删除持仓"""
    from backend.services.portfolio_write_service import PortfolioWriteService
    from backend.api.portfolio_ai import reload_agent_portfolio_provider

    result = PortfolioWriteService().safe_remove_position(symbol)
    reload_agent_portfolio_provider()
    return {**result, "message": f"已删除持仓: {symbol}"}


@router.post("/refresh")
async def refresh_portfolio():
    """刷新持仓价格"""
    service = get_service()
    await service.refresh_portfolio()
    return {"message": "持仓价格已刷新"}


@router.get("/summary")
async def get_portfolio_summary():
    """获取持仓摘要"""
    service = get_service()
    portfolio = await service.get_portfolio()

    # 计算摘要统计
    positions = portfolio.get("positions", [])
    total_profit = portfolio.get("total_profit", 0)
    total_assets = portfolio.get("total_assets", 0)

    # 按盈亏排序
    winners = [p for p in positions if p.get("profit", 0) > 0]
    losers = [p for p in positions if p.get("profit", 0) < 0]

    return {
        "total_positions": len(positions),
        "total_assets": total_assets,
        "cash": portfolio.get("cash", 0),
        "market_value": portfolio.get("total_market_value", 0),
        "total_profit": total_profit,
        "profit_pct": (total_profit / (total_assets - total_profit) * 100) if (total_assets - total_profit) > 0 else 0,
        "winners_count": len(winners),
        "losers_count": len(losers),
        "top_winner": max(positions, key=lambda x: x.get("profit_pct", 0)) if positions else None,
        "top_loser": min(positions, key=lambda x: x.get("profit_pct", 0)) if positions else None,
    }
