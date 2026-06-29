import uuid
from datetime import datetime

from pydantic import BaseModel


class NarrativeUnitResponse(BaseModel):
    id: uuid.UUID
    theme_id: uuid.UUID
    source_page: int | None
    fields: dict
    confidence: float | None
    generation: int
    is_latest: bool
    is_manual: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class NarrativeUnitCreate(BaseModel):
    source_page: int | None = None
    fields: dict
    confidence: float | None = None


class NarrativeUnitUpdate(BaseModel):
    fields: dict | None = None
    confidence: float | None = None
