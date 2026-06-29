from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator


class LLMProvider(ABC):
    @abstractmethod
    async def chat(self, messages: list[dict], stream: bool = False):
        """对话，支持流式。stream=True 时返回 AsyncGenerator，否则返回 str"""
        pass

    @abstractmethod
    async def chat_stream(self, messages: list[dict]) -> AsyncGenerator[str, None]:
        """流式对话，逐块返回文本"""
        pass
