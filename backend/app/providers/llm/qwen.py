import asyncio
from collections.abc import AsyncGenerator

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from app.config import settings
from app.providers.llm.base import LLMProvider


class OpenAICompatibleLLM(LLMProvider):
    """OpenAI 兼容 LLM 适配器（适用于通义千问、OpenAI 等所有兼容接口）"""

    def __init__(self):
        self.client = AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout=60.0,
            max_retries=0,
        )
        self.model = settings.llm_model
        self.provider = settings.llm_provider
        self.max_retries = 2

    async def chat(self, messages: list[dict], stream: bool = False):
        if stream:
            return self.chat_stream(messages)

        for attempt in range(self.max_retries + 1):
            try:
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=4096,
                    temperature=0.0,
                    **_extra_completion_kwargs(self.provider, self.model),
                )
                content = response.choices[0].message.content
                if content is None:
                    raise ValueError("LLM 返回空响应，可能是内容被安全过滤")
                return content
            except (APIConnectionError, APITimeoutError) as exc:
                if attempt >= self.max_retries:
                    raise RuntimeError(_format_llm_error(exc)) from exc
                await asyncio.sleep(1.5 * (attempt + 1))
            except APIStatusError as exc:
                raise RuntimeError(_format_llm_error(exc)) from exc

    async def chat_stream(self, messages: list[dict]) -> AsyncGenerator[str, None]:
        for attempt in range(self.max_retries + 1):
            try:
                stream = await self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=4096,
                    temperature=0.0,
                    stream=True,
                    **_extra_completion_kwargs(self.provider, self.model),
                )
                async for chunk in stream:
                    if chunk.choices and chunk.choices[0].delta.content:
                        yield chunk.choices[0].delta.content
                return
            except (APIConnectionError, APITimeoutError) as exc:
                if attempt >= self.max_retries:
                    raise RuntimeError(_format_llm_error(exc)) from exc
                await asyncio.sleep(1.5 * (attempt + 1))
            except APIStatusError as exc:
                raise RuntimeError(_format_llm_error(exc)) from exc


class RuntimeLLMProvider(LLMProvider):
    """运行时配置的 LLM 适配器，base_url 从厂商注册表推断，其余从前端传入"""

    def __init__(self, config: dict):
        provider = (config.get("provider") or "").strip()
        api_key = (config.get("api_key") or "").strip()
        model = (config.get("model") or "").strip()

        # 从同一个厂商注册表推断 base_url
        base_url = _resolve_base_url(provider)

        self.client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=60.0,
            max_retries=0,
        )
        self.provider = provider
        self.model = model
        self.max_retries = 2

    async def chat(self, messages: list[dict], stream: bool = False):
        if stream:
            return self.chat_stream(messages)

        for attempt in range(self.max_retries + 1):
            try:
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=4096,
                    temperature=0.0,
                    **_extra_completion_kwargs(self.provider, self.model),
                )
                content = response.choices[0].message.content
                if content is None:
                    raise ValueError("LLM 返回空响应，可能是内容被安全过滤")
                return content
            except (APIConnectionError, APITimeoutError) as exc:
                if attempt >= self.max_retries:
                    raise RuntimeError(_format_llm_error(exc)) from exc
                await asyncio.sleep(1.5 * (attempt + 1))
            except APIStatusError as exc:
                raise RuntimeError(_format_llm_error(exc)) from exc

    async def chat_stream(self, messages: list[dict]) -> AsyncGenerator[str, None]:
        for attempt in range(self.max_retries + 1):
            try:
                stream = await self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=4096,
                    temperature=0.0,
                    stream=True,
                    **_extra_completion_kwargs(self.provider, self.model),
                )
                async for chunk in stream:
                    if chunk.choices and chunk.choices[0].delta.content:
                        yield chunk.choices[0].delta.content
                return
            except (APIConnectionError, APITimeoutError) as exc:
                if attempt >= self.max_retries:
                    raise RuntimeError(_format_llm_error(exc)) from exc
                await asyncio.sleep(1.5 * (attempt + 1))
            except APIStatusError as exc:
                raise RuntimeError(_format_llm_error(exc)) from exc


def _resolve_base_url(provider: str) -> str:
    """从厂商 ID 推断 base_url，复用 ocr_config_service 中的注册表"""
    try:
        from app.services.ocr_config_service import PROVIDERS
        cfg = PROVIDERS.get(provider)
        if cfg:
            return cfg["base_url"]
    except Exception:
        pass
    # fallback: 用 .env 里的 LLM base_url
    return settings.llm_base_url


def _extra_completion_kwargs(provider: str, model: str) -> dict:
    """DashScope Qwen thinking models can be slow by default; disable thinking for structured jobs."""
    provider_id = (provider or "").lower()
    model_id = (model or "").lower()
    if provider_id == "dashscope" and model_id.startswith("qwen3") and "vl" not in model_id:
        return {"extra_body": {"enable_thinking": False}}
    return {}


def _format_llm_error(exc: Exception) -> str:
    if isinstance(exc, APITimeoutError):
        return "模型服务响应超时，请稍后重试，或减少单次对话内容后再试。"
    if isinstance(exc, APIConnectionError):
        return "模型服务连接失败，请检查网络、模型服务地址、API Key，以及模型服务商当前是否可用。"
    if isinstance(exc, APIStatusError):
        return f"模型服务返回错误 {exc.status_code}，请检查模型配置和服务商返回信息。"
    return str(exc)


# 保持向后兼容的别名
QwenLLM = OpenAICompatibleLLM
