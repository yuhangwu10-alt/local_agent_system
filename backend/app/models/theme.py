import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ThemeConfig(Base):
    __tablename__ = "theme_config"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("project.id", ondelete="CASCADE"))
    theme: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    keywords: Mapped[dict | None] = mapped_column(JSONB)
    page_pool_rule: Mapped[str | None] = mapped_column(Text)
    narrative_schema: Mapped[list | None] = mapped_column(JSONB)
    page_pool_prompt: Mapped[str | None] = mapped_column(Text)
    narrative_prompt: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="drafting")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    project: Mapped["Project"] = relationship(back_populates="themes")
    pool_entries: Mapped[list["PagePool"]] = relationship(back_populates="theme", cascade="all, delete-orphan")
    narrative_units: Mapped[list["NarrativeUnit"]] = relationship(back_populates="theme", cascade="all, delete-orphan")
    chat_sessions: Mapped[list["ChatSession"]] = relationship(back_populates="theme")
