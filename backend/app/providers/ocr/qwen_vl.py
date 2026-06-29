import asyncio
import base64

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from app.config import settings
from app.providers.ocr.base import OCRProvider
from app.services.ocr_config_service import PROVIDERS

OCR_PROMPT = """请严格按照以下规则识别古籍页面中的文字：

1. 版面结构识别：
   - 识别页面中的列结构（通常为竖排）
   - 不允许跨列合并文字

2. 阅读顺序规则：
   - 从最右侧的列开始，逐列向左阅读
   - 单列内从上到下阅读

3. 内容过滤规则：
   - 仅输出中文繁体文字
   - 禁止输出英文、阿拉伯数字、标点符号
   - 禁止补全或推测模糊文字

4. 输出格式要求：
   - 每列文字占一行
   - 不保留空格
   - 不添加序号或标点

如果页面无文字，仅输出"无文字"。"""


class OpenAICompatibleOCR(OCRProvider):
    """OpenAI 兼容视觉 OCR 适配器（适用于通义千问 VL、GPT-4V 等）"""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        prompt: str | None = None,
        extra_headers: dict | None = None,
    ):
        self.client = AsyncOpenAI(
            base_url=base_url or settings.ocr_base_url,
            api_key=api_key or settings.ocr_api_key,
            default_headers=extra_headers or None,
            timeout=settings.ocr_request_timeout,
        )
        self.model = model or settings.ocr_model
        self.prompt = (prompt or "").strip() or OCR_PROMPT

    @classmethod
    def from_runtime_config(cls, config: dict | None):
        if not config:
            return cls()
        provider_id = config.get("provider")
        provider_config = PROVIDERS.get(provider_id or "")
        if not provider_config:
            return cls(
                base_url=config.get("base_url"),
                api_key=config.get("api_key"),
                model=config.get("model"),
                prompt=config.get("prompt"),
            )
        return cls(
            base_url=provider_config["base_url"],
            api_key=config.get("api_key"),
            model=config.get("model"),
            prompt=config.get("prompt"),
            extra_headers=provider_config.get("extra_headers") or None,
        )

    async def recognize(self, image_bytes: bytes) -> tuple[str, float | None]:
        base64_image = base64.b64encode(image_bytes).decode("utf-8")
        data_url = f"data:image/jpeg;base64,{base64_image}"

        last_error: Exception | None = None
        for attempt in range(max(settings.ocr_request_retries, 1)):
            try:
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": self.prompt},
                                {"type": "image_url", "image_url": {"url": data_url}},
                            ],
                        }
                    ],
                    max_tokens=4096,
                )
                text = response.choices[0].message.content.strip()
                return text, None  # 不伪造置信度
            except (APIConnectionError, APITimeoutError) as exc:
                last_error = exc
                if attempt < settings.ocr_request_retries - 1:
                    await asyncio.sleep(settings.ocr_retry_delay_seconds * (attempt + 1))
                    continue
                raise RuntimeError(_format_ocr_error(exc)) from exc
            except APIStatusError as exc:
                raise RuntimeError(_format_ocr_error(exc)) from exc

        raise RuntimeError(_format_ocr_error(last_error))


def _format_ocr_error(exc: Exception | None) -> str:
    if isinstance(exc, APITimeoutError):
        return "OCR 模型响应超时，已按配置重试后仍失败。"
    if isinstance(exc, APIConnectionError):
        return "OCR 模型服务连接失败，已按配置重试后仍失败。"
    if isinstance(exc, APIStatusError):
        return f"OCR 模型服务返回错误 {exc.status_code}。"
    return str(exc) if exc else "OCR 模型调用失败。"


# 保持向后兼容的别名
QwenVLOCR = OpenAICompatibleOCR
