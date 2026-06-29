import uuid
from datetime import datetime

from pydantic import BaseModel


class ProjectCreate(BaseModel):
    name: str
    description: str | None = None


class ProjectUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


class ProjectResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DocumentResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    file_type: str
    file_path: str
    file_name: str
    total_pages: int | None
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ScanResponse(BaseModel):
    new_files: list[DocumentResponse]
    count: int
