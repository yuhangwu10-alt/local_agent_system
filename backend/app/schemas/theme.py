import uuid
from datetime import datetime

from pydantic import BaseModel


class ThemeConfigCreate(BaseModel):
    theme: str
    description: str | None = None
    keywords: dict | None = None
    page_pool_rule: str | None = None
    narrative_schema: list[str] | None = None
    page_pool_prompt: str | None = None
    narrative_prompt: str | None = None


class ThemeConfigUpdate(BaseModel):
    theme: str | None = None
    description: str | None = None
    keywords: dict | None = None
    page_pool_rule: str | None = None
    narrative_schema: list[str] | None = None
    page_pool_prompt: str | None = None
    narrative_prompt: str | None = None
    status: str | None = None


class ThemeConfigResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    theme: str
    description: str | None
    keywords: dict | None
    page_pool_rule: str | None
    narrative_schema: list[str] | None
    page_pool_prompt: str | None
    narrative_prompt: str | None
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
