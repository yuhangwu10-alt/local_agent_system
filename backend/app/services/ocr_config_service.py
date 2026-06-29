from typing import Dict

from openai import AsyncOpenAI


PROVIDERS: Dict[str, Dict] = {
    "dashscope": {
        "name": "阿里云百炼",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "extra_headers": {"X-DashScope-SSE": "enable"},
    },
    "mimo": {
        "name": "小米 MiMo",
        "base_url": "https://token-plan-cn.xiaomimimo.com/v1",
        "extra_headers": {},
    },
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "extra_headers": {},
    },
    "glm": {
        "name": "智谱 (GLM)",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "extra_headers": {},
    },
    "kimi": {
        "name": "月之暗面 (Kimi)",
        "base_url": "https://api.moonshot.cn/v1",
        "extra_headers": {},
        "default_temperature": 1.0,
    },
    "siliconflow": {
        "name": "硅基流动",
        "base_url": "https://api.siliconflow.cn/v1",
        "extra_headers": {},
    },
}


def public_providers() -> list[dict]:
    return [
        {
            "id": provider_id,
            "name": config["name"],
            "base_url": config["base_url"],
        }
        for provider_id, config in PROVIDERS.items()
    ]


async def list_provider_models(provider_id: str, api_key: str) -> list[dict]:
    config = PROVIDERS.get(provider_id)
    if not config:
        raise ValueError(f"不支持的 OCR 厂商: {provider_id}")
    if not api_key.strip():
        raise ValueError("请先填写 API Key")

    client = AsyncOpenAI(
        base_url=config["base_url"],
        api_key=api_key.strip(),
        default_headers=config.get("extra_headers") or None,
        timeout=30.0,
    )
    models = await client.models.list()
    result = []
    for model in models.data:
        model_id = getattr(model, "id", None)
        if model_id:
            result.append({"id": model_id, "name": model_id})
    return result
