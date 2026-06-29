import uuid
from datetime import datetime

from pydantic import BaseModel


class PageContentResponse(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    page_no: int
    raw_ocr_text: str | None
    content: str | None
    ocr_status: str
    ocr_provider: str | None
    ocr_confidence: float | None
    classification: dict | None = None

    model_config = {"from_attributes": True}


class PageContentUpdate(BaseModel):
    content: str


class PagePoolResponse(BaseModel):
    id: uuid.UUID
    theme_id: uuid.UUID
    page_id: uuid.UUID
    score: float | None
    relevance_level: str | None
    reason: str | None
    命中关键词: list | None = None
    命中规则: dict | None = None
    关键词命中数: int | None = None
    信号命中数: int | None = None
    置信度: str | None = None
    generation: int
    is_latest: bool
    is_manual: bool

    model_config = {"from_attributes": True}


class PagePoolCreate(BaseModel):
    page_id: uuid.UUID
    score: float | None = None
    relevance_level: str | None = None
    reason: str | None = None


class PagePoolUpdate(BaseModel):
    score: float | None = None
    relevance_level: str | None = None
    reason: str | None = None
