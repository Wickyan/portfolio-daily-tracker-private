"""持仓API路由。"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

router = APIRouter()


class AddPositionRequest(BaseModel):
    """使用规范字段添加持仓。"""
    account: str
    code: str
    name: str
    currency: str
    asset_type: str
    quantity: float
    cost_price: float
    total_cost: Optional[float] = None
    fee: Optional[float] = None
    note: str = ""
    source: str = "manual"


class UpdatePositionRequest(BaseModel):
    """account+code+currency共同定位持仓。"""
    account: str
    currency: str
    quantity: Optional[float] = None
    cost_price: Optional[float] = None


def get_service():
    """获取Agent服务实例。"""
    from backend.main import get_agent_service

    service = get_agent_service()
    if service is None:
        raise HTTPException(status_code=503, detail="服务尚未初始化")
    return service


@router.get("")
async def get_portfolio():
    service = get_service()
    return await service.get_portfolio()


@router.get("/live")
async def get_live_portfolio():
    service = get_service()
    await service.refresh_portfolio()
    return await service.get_portfolio()


@router.post("/add")
async def add_position(request: AddPositionRequest):
    from backend.api.portfolio_ai import reload_agent_portfolio_provider
    from backend.services.portfolio_write_service import PortfolioWriteService

    write_service = PortfolioWriteService()
    change = request.model_dump()
    missing = write_service.validate_confirmable_change(change)
    if missing:
        raise HTTPException(status_code=400, detail=f"缺少字段: {', '.join(missing)}")

    try:
        result = write_service.safe_add_positions(
            [change],
            summary=f"手动新增持仓: {request.account}/{request.name}",
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    reload_agent_portfolio_provider()
    return {
        **result,
        "message": f"已添加持仓: {request.account}/{request.name}({request.code})",
    }


@router.put("/{code}")
async def update_position(code: str, request: UpdatePositionRequest):
    from backend.api.portfolio_ai import reload_agent_portfolio_provider
    from backend.services.portfolio_write_service import PortfolioWriteService

    if request.quantity is None and request.cost_price is None:
        raise HTTPException(status_code=400, detail="至少提供quantity或cost_price")

    try:
        result = PortfolioWriteService().safe_update_position(
            account=request.account,
            code=code,
            currency=request.currency,
            updates={
                "quantity": request.quantity,
                "cost_price": request.cost_price,
            },
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="持仓不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    reload_agent_portfolio_provider()
    return {
        **result,
        "message": f"已更新持仓: {request.account}/{code}/{request.currency.upper()}",
    }


@router.delete("/{code}")
async def remove_position(
    code: str,
    account: str = Query(..., min_length=1),
    currency: str = Query(..., min_length=3),
):
    from backend.api.portfolio_ai import reload_agent_portfolio_provider
    from backend.services.portfolio_write_service import PortfolioWriteService

    try:
        result = PortfolioWriteService().safe_remove_position(
            account=account,
            code=code,
            currency=currency,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="持仓不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    reload_agent_portfolio_provider()
    return {
        **result,
        "message": f"已删除持仓: {account}/{code}/{currency.upper()}",
    }


@router.post("/refresh")
async def refresh_portfolio():
    service = get_service()
    await service.refresh_portfolio()
    return {"message": "持仓价格已刷新"}


@router.get("/summary")
async def get_portfolio_summary():
    service = get_service()
    portfolio = await service.get_portfolio()

    positions = portfolio.get("positions", [])
    total_profit = portfolio.get("total_profit", 0)
    total_assets = portfolio.get("total_assets", 0)
    winners = [p for p in positions if p.get("profit", 0) > 0]
    losers = [p for p in positions if p.get("profit", 0) < 0]

    return {
        "total_positions": len(positions),
        "total_assets": total_assets,
        "cash": portfolio.get("cash", 0),
        "market_value": portfolio.get("total_market_value", 0),
        "total_profit": total_profit,
        "profit_pct": (
            total_profit / (total_assets - total_profit) * 100
            if (total_assets - total_profit) > 0
            else 0
        ),
        "winners_count": len(winners),
        "losers_count": len(losers),
        "top_winner": max(positions, key=lambda x: x.get("profit_pct", 0)) if positions else None,
        "top_loser": min(positions, key=lambda x: x.get("profit_pct", 0)) if positions else None,
    }
