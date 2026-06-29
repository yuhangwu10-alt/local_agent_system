from collections.abc import AsyncGenerator

from app.config import settings
from app.providers.llm.base import LLMProvider
from app.providers.llm.qwen import OpenAICompatibleLLM, RuntimeLLMProvider


def _with_custom_prompt(messages: list[dict], runtime_config: dict | None = None) -> list[dict]:
    prompt = ""
    if isinstance(runtime_config, dict):
        prompt = str(runtime_config.get("prompt") or "").strip()
    if not prompt:
        return messages

    custom_message = {
        "role": "system",
        "content": f"用户自定义补充提示词：\n{prompt}",
    }
    result = list(messages)
    if result and result[0].get("role") == "system":
        return [result[0], custom_message, *result[1:]]
    return [custom_message, *result]

# Provider 单例缓存（使用 .env 默认配置）
_llm_provider_instance: LLMProvider | None = None

# RuntimeLLMProvider 缓存，按 (provider, api_key, model) 复用实例和底层 httpx 连接池
_runtime_providers: dict[tuple[str, str, str], RuntimeLLMProvider] = {}


def get_llm_provider() -> LLMProvider:
    global _llm_provider_instance
    if _llm_provider_instance is None:
        _llm_provider_instance = OpenAICompatibleLLM()
    return _llm_provider_instance


def _get_runtime_provider(runtime_config: dict) -> RuntimeLLMProvider:
    """按 (provider, api_key, model) 缓存 RuntimeLLMProvider，复用 httpx 连接池"""
    provider = (runtime_config.get("provider") or "").strip()
    api_key = (runtime_config.get("api_key") or "").strip()
    model = (runtime_config.get("model") or "").strip()
    key = (provider, api_key, model)
    if key not in _runtime_providers:
        _runtime_providers[key] = RuntimeLLMProvider(runtime_config)
    return _runtime_providers[key]


async def chat(messages: list[dict], stream: bool = False, runtime_config: dict | None = None):
    """调用 LLM 对话。runtime_config 可包含 provider / api_key / model 覆盖默认配置。"""
    messages = _with_custom_prompt(messages, runtime_config)
    if runtime_config and (runtime_config.get("api_key") or runtime_config.get("model")):
        provider = _get_runtime_provider(runtime_config)
    else:
        provider = get_llm_provider()
    return await provider.chat(messages, stream=stream)


async def chat_stream(messages: list[dict], runtime_config: dict | None = None) -> AsyncGenerator[str, None]:
    """流式调用 LLM。runtime_config 可包含 provider / api_key / model 覆盖默认配置。"""
    messages = _with_custom_prompt(messages, runtime_config)
    if runtime_config and (runtime_config.get("api_key") or runtime_config.get("model")):
        provider = _get_runtime_provider(runtime_config)
    else:
        provider = get_llm_provider()
    async for chunk in provider.chat_stream(messages):
        yield chunk
