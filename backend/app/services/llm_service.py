from collections.abc import AsyncGenerator
import asyncio

from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.models.platform import ModelProfile
from app.providers.llm.base import LLMProvider
from app.providers.llm.qwen import OpenAICompatibleLLM, RuntimeLLMProvider


def _with_custom_prompt(messages: list[dict], runtime_config: dict | None = None) -> list[dict]:
    prompt = ""
    if isinstance(runtime_config, dict):
        prompt = str(runtime_config.get("prompt") or "").strip()
    if not prompt:
        return messages
    custom_message = {"role": "system", "content": f"用户自定义补充提示词：\n{prompt}"}
    result = list(messages)
    if result and result[0].get("role") == "system":
        return [result[0], custom_message, *result[1:]]
    return [custom_message, *result]


_llm_provider_instance: LLMProvider | None = None
_runtime_providers: dict[tuple[str, str, str, str, int, int], RuntimeLLMProvider] = {}
_profile_semaphores: dict[str, tuple[int, asyncio.Semaphore]] = {}


def get_llm_provider() -> LLMProvider:
    global _llm_provider_instance
    if _llm_provider_instance is None:
        _llm_provider_instance = OpenAICompatibleLLM()
    return _llm_provider_instance


def _get_runtime_provider(runtime_config: dict) -> RuntimeLLMProvider:
    provider = (runtime_config.get("provider") or "").strip()
    api_key = (runtime_config.get("api_key") or "").strip()
    model = (runtime_config.get("model") or "").strip()
    key = (
        provider,
        api_key,
        model,
        str(runtime_config.get("base_url") or "").strip(),
        int(runtime_config.get("timeout_seconds") or 0),
        int(runtime_config.get("retries") or 0),
    )
    if key not in _runtime_providers:
        _runtime_providers[key] = RuntimeLLMProvider(runtime_config)
    return _runtime_providers[key]


async def _platform_config(stage: str | None = None) -> tuple[dict | None, asyncio.Semaphore | None]:
    async with async_session() as db:
        result = await db.execute(
            select(ModelProfile)
            .where(ModelProfile.enabled.is_(True))
            .order_by(ModelProfile.priority, ModelProfile.created_at)
        )
        profiles = result.scalars().all()
    selected = None
    if stage:
        selected = next(
            (item for item in profiles if stage in (item.stages or [])),
            None,
        )
        if selected is None:
            # OCR is multimodal and must never silently fall back to a
            # text-only channel. Other stages may use an explicitly generic
            # profile as their normal fallback.
            if stage != "ocr":
                generic = [item for item in profiles if not (item.stages or [])]
                selected = next((item for item in generic if item.is_default), None) or (generic[0] if generic else None)
    else:
        selected = next((item for item in profiles if item.is_default), None) or (profiles[0] if profiles else None)
    if selected is None:
        return None, None
    key = str(selected.id)
    concurrency = max(1, int(selected.max_concurrency or 1))
    cached = _profile_semaphores.get(key)
    if cached is None or cached[0] != concurrency:
        semaphore = asyncio.Semaphore(concurrency)
        _profile_semaphores[key] = (concurrency, semaphore)
    else:
        semaphore = cached[1]
    return {
        "provider": selected.provider,
        "base_url": selected.base_url,
        "api_key": selected.api_key,
        "model": selected.model,
        "timeout_seconds": selected.timeout_seconds,
        "retries": selected.retries,
    }, semaphore


async def get_platform_model_config(stage: str | None = None) -> tuple[dict | None, asyncio.Semaphore | None]:
    """Return an administrator-managed model channel and its concurrency gate."""
    return await _platform_config(stage)


async def chat(
    messages: list[dict],
    stream: bool = False,
    runtime_config: dict | None = None,
    *,
    stage: str = "chat",
):
    # Provider, credentials, model and concurrency always come from administrator profiles.
    prompt = runtime_config.get("prompt") if isinstance(runtime_config, dict) else None
    config, semaphore = await _platform_config(stage)
    messages = _with_custom_prompt(messages, {"prompt": prompt} if prompt else None)
    provider = _get_runtime_provider(config) if config else get_llm_provider()
    if semaphore is None:
        return await provider.chat(messages, stream=stream)
    async with semaphore:
        return await provider.chat(messages, stream=stream)


async def chat_stream(
    messages: list[dict],
    runtime_config: dict | None = None,
    *,
    stage: str = "chat",
) -> AsyncGenerator[str, None]:
    prompt = runtime_config.get("prompt") if isinstance(runtime_config, dict) else None
    config, semaphore = await _platform_config(stage)
    messages = _with_custom_prompt(messages, {"prompt": prompt} if prompt else None)
    provider = _get_runtime_provider(config) if config else get_llm_provider()
    if semaphore is None:
        async for chunk in provider.chat_stream(messages):
            yield chunk
        return
    async with semaphore:
        async for chunk in provider.chat_stream(messages):
            yield chunk
