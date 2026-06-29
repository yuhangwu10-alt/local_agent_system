from abc import ABC, abstractmethod


class OCRProvider(ABC):
    @abstractmethod
    async def recognize(self, image_bytes: bytes) -> tuple[str, float | None]:
        """识别单张图片，返回 (文本, 置信度)"""
        pass
