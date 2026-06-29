import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class PageContent(Base):
    __tablename__ = "page_content"
    __table_args__ = (UniqueConstraint("document_id", "page_no"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("source_document.id", ondelete="CASCADE"))
    page_no: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_ocr_text: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    ocr_status: Mapped[str] = mapped_column(String(20), default="pending")
    ocr_provider: Mapped[str | None] = mapped_column(String(50))
    ocr_confidence: Mapped[float | None] = mapped_column(Float)
    ocr_error: Mapped[str | None] = mapped_column(Text)
    classification: Mapped[dict | None] = mapped_column(JSONB)  # 页级分类结果
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    document: Mapped["SourceDocument"] = relationship(back_populates="pages")
    pool_entries: Mapped[list["PagePool"]] = relationship(back_populates="page")


class PagePool(Base):
    __tablename__ = "page_pool"
    __table_args__ = (UniqueConstraint("theme_id", "page_id", "generation"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    theme_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("theme_config.id", ondelete="CASCADE"))
    page_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("page_content.id", ondelete="CASCADE"))
    score: Mapped[float | None] = mapped_column(Float)
    relevance_level: Mapped[str | None] = mapped_column(String(20))
    reason: Mapped[str | None] = mapped_column(Text)
    命中关键词: Mapped[list | None] = mapped_column("命中关键词", JSONB)
    命中规则: Mapped[dict | None] = mapped_column("命中规则", JSONB)
    关键词命中数: Mapped[int | None] = mapped_column("关键词命中数", Integer)
    信号命中数: Mapped[int | None] = mapped_column("信号命中数", Integer)
    置信度: Mapped[str | None] = mapped_column("置信度", String(10))
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_latest: Mapped[bool] = mapped_column(Boolean, default=True)
    is_manual: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    page: Mapped["PageContent"] = relationship(back_populates="pool_entries")
    theme: Mapped["ThemeConfig"] = relationship(back_populates="pool_entries")
