import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class ChatSessionCreate(BaseModel):
    title: str | None = None
    theme_id: uuid.UUID | None = None


class ChatSessionResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    theme_id: uuid.UUID | None
    title: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ChatMessageCreate(BaseModel):
    content: str
    document_id: uuid.UUID | None = None
    llm_config: dict | None = None
    topic_context: list[dict] | None = None


class ChatLocalMessageCreate(BaseModel):
    role: Literal["user", "assistant"] = "assistant"
    content: str
    metadata_: dict | None = None


class ChatMessageResponse(BaseModel):
    id: uuid.UUID
    session_id: uuid.UUID
    role: str
    content: str
    metadata_: dict | None = None
    created_at: datetime

    model_config = {"from_attributes": True}
