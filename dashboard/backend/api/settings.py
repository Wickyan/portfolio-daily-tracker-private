"""
配置和设置 API
"""
import httpx
from fastapi import APIRouter, Depends
from typing import List, Dict, Any
from pydantic import BaseModel

from config import get_config

router = APIRouter()


# 延迟导入避免循环依赖
def get_agent_service():
    from backend.main import agent_service
    if agent_service is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="服务尚未初始化")
    return agent_service


class ModelInfo(BaseModel):
    id: str
    name: str
    description: str
    supports_vision: bool = False


class APIGroupInfo(BaseModel):
    name: str
    description: str
    models: List[ModelInfo]


class ConfigResponse(BaseModel):
    current_api_group: str
    current_model: str
    api_groups: Dict[str, APIGroupInfo]
    available_models: List[ModelInfo]


def _mask_api_key(api_key: str) -> str:
    if not api_key:
        return ""
    if len(api_key) <= 4:
        return "****"
    return f"********{api_key[-4:]}"


def _is_masked_api_key(api_key: str) -> bool:
    return bool(api_key) and (set(api_key) == {"*"} or api_key.startswith("********"))


def _safe_error_message(error: Exception, api_key: str = "") -> str:
    message = str(error).strip() or "连接失败"
    if api_key:
        message = message.replace(api_key, "[REDACTED]")
    return message


@router.get("/config", response_model=ConfigResponse)
async def get_config_info():
    """获取配置信息"""
    config = get_config()
    
    # 获取所有 API 组
    api_groups_data = config.get_all_api_groups()
    api_groups = {}
    
    for key, group in api_groups_data.items():
        api_groups[key] = APIGroupInfo(
            name=group.get("name", ""),
            description=group.get("description", ""),
            models=[
                ModelInfo(
                    id=m["id"],
                    name=m["name"],
                    description=m.get("description", ""),
                    supports_vision=m.get("supports_vision", False)
                )
                for m in group.get("models", [])
            ]
        )
    
    # 获取当前配置
    current_api_group = config.get("current_api_group", "xhub")
    current_model = config.get_current_model()
    available_models = [
        ModelInfo(
            id=m["id"],
            name=m["name"],
            description=m.get("description", ""),
            supports_vision=m.get("supports_vision", False)
        )
        for m in config.get_available_models()
    ]
    
    return ConfigResponse(
        current_api_group=current_api_group,
        current_model=current_model,
        api_groups=api_groups,
        available_models=available_models
    )


class SwitchModelRequest(BaseModel):
    model_id: str


@router.post("/model")
async def switch_model(request: SwitchModelRequest, service = Depends(get_agent_service)):
    """切换模型"""
    config = get_config()
    success = config.switch_model(request.model_id)
    
    if success:
        # 热重载LLM配置
        await service.reload_llm()
        return {"success": True, "model": request.model_id, "reloaded": True}
    else:
        return {"success": False, "error": "模型不可用"}


class SwitchAPIGroupRequest(BaseModel):
    group_name: str


@router.post("/api-group")
async def switch_api_group(request: SwitchAPIGroupRequest, service = Depends(get_agent_service)):
    """切换 API 组"""
    config = get_config()
    success = config.switch_api_group(request.group_name)
    
    if success:
        # 热重载LLM配置
        await service.reload_llm()
        return {"success": True, "group": request.group_name, "reloaded": True}
    else:
        return {"success": False, "error": "API 组不存在"}


class UpdateCashRequest(BaseModel):
    cash: float


@router.post("/cash")
async def update_cash(request: UpdateCashRequest, service = Depends(get_agent_service)):
    """更新现金"""
    service.portfolio_provider.set_cash(request.cash)
    return {"success": True, "cash": request.cash}


class LLMConfigResponse(BaseModel):
    api_group: str
    base_url: str
    model: str
    api_key_configured: bool
    api_key_masked: str


class LLMConfigRequest(BaseModel):
    api_group: str = "deepseek"
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str | None = None
    model: str = "deepseek-chat"


class LLMTestResponse(BaseModel):
    ok: bool
    message: str


def _get_group_config(config, api_group: str) -> Dict[str, Any]:
    groups = config.get_all_api_groups()
    return groups.get(api_group, {})


def _model_entry(model_id: str) -> Dict[str, Any]:
    return {
        "id": model_id,
        "name": model_id,
        "description": "OpenAI-compatible chat model",
        "supports_vision": False
    }


def _save_llm_config(config, request: LLMConfigRequest) -> Dict[str, Any]:
    api_group = request.api_group.strip() or "deepseek"
    base_url = request.base_url.strip().rstrip("/") or "https://api.deepseek.com/v1"
    model = request.model.strip() or "deepseek-chat"
    existing_group = _get_group_config(config, api_group)
    existing_key = existing_group.get("api_key", "")
    incoming_key = (request.api_key or "").strip()

    api_key = existing_key
    if incoming_key and not _is_masked_api_key(incoming_key):
        api_key = incoming_key

    models = existing_group.get("models", [])
    if not any(m.get("id") == model for m in models):
        models = [_model_entry(model)]

    group_config = {
        **existing_group,
        "name": existing_group.get("name") or ("DeepSeek" if api_group == "deepseek" else "OpenAI-Compatible API"),
        "description": existing_group.get("description") or "OpenAI-compatible API endpoint",
        "base_url": base_url,
        "api_key": api_key,
        "headers": None,
        "provider_type": "openai",
        "models": models,
    }

    if api_group == "deepseek":
        group_config["name"] = "DeepSeek"
        group_config["description"] = "DeepSeek OpenAI-compatible API"
        group_config["models"] = [{
            "id": model,
            "name": "DeepSeek Chat" if model == "deepseek-chat" else model,
            "description": "DeepSeek chat model",
            "supports_vision": False
        }]

    config.set(f"api_groups.{api_group}", group_config)
    config.set("current_api_group", api_group)
    config.set("llm.current_model", model)
    return group_config


@router.get("/llm", response_model=LLMConfigResponse)
async def get_llm_config():
    """获取当前 LLM 配置，不返回明文 API Key"""
    config = get_config()
    current_group = config.get("current_api_group", "deepseek")
    api_group = current_group if current_group in ("deepseek", "openai_compatible") else "deepseek"
    group = _get_group_config(config, api_group)
    api_key = group.get("api_key", "")
    default_model = "deepseek-chat" if api_group == "deepseek" else config.get_current_model()
    model = group.get("models", [{}])[0].get("id", default_model) if current_group != api_group else config.get_current_model()

    return LLMConfigResponse(
        api_group=api_group,
        base_url=group.get("base_url", "https://api.deepseek.com/v1"),
        model=model,
        api_key_configured=bool(api_key),
        api_key_masked=_mask_api_key(api_key),
    )


@router.post("/llm", response_model=LLMConfigResponse)
async def save_llm_config(request: LLMConfigRequest, service = Depends(get_agent_service)):
    """保存 LLM 配置并热重载，不记录或返回明文 API Key"""
    config = get_config()
    group = _save_llm_config(config, request)
    await service.reload_llm()
    api_key = group.get("api_key", "")

    return LLMConfigResponse(
        api_group=request.api_group,
        base_url=group.get("base_url", ""),
        model=request.model,
        api_key_configured=bool(api_key),
        api_key_masked=_mask_api_key(api_key),
    )


@router.post("/llm/test", response_model=LLMTestResponse)
async def test_llm_config(request: LLMConfigRequest):
    """测试 OpenAI-compatible chat completions 连接，不保存配置"""
    base_url = request.base_url.strip().rstrip("/")
    api_key = (request.api_key or "").strip()
    model = request.model.strip() or "deepseek-chat"

    if not base_url:
        return LLMTestResponse(ok=False, message="Base URL 不能为空")
    if not api_key or _is_masked_api_key(api_key):
        group = _get_group_config(get_config(), request.api_group)
        api_key = group.get("api_key", "")
    if not api_key:
        return LLMTestResponse(ok=False, message="API Key 不能为空")

    url = f"{base_url}/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8,
        "temperature": 0,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
            if not data.get("choices"):
                return LLMTestResponse(ok=False, message="响应缺少 choices")
    except Exception as e:
        return LLMTestResponse(ok=False, message=_safe_error_message(e, api_key))

    return LLMTestResponse(ok=True, message="连接成功")
