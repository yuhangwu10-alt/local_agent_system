"""initial schema

Revision ID: 001
Revises:
Create Date: 2025-05-31
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # project
    op.create_table(
        "project",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("status", sa.String(20), server_default="created"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # source_document
    op.create_table(
        "source_document",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("project.id", ondelete="CASCADE"), nullable=False),
        sa.Column("file_type", sa.String(10), nullable=False),
        sa.Column("file_path", sa.Text, nullable=False),
        sa.Column("file_name", sa.String(500), nullable=False),
        sa.Column("total_pages", sa.Integer),
        sa.Column("status", sa.String(20), server_default="registered"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_source_document_project_id", "source_document", ["project_id"])

    # theme_config
    op.create_table(
        "theme_config",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("project.id", ondelete="CASCADE"), nullable=False),
        sa.Column("theme", sa.String(200), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("keywords", JSONB),
        sa.Column("page_pool_rule", sa.Text),
        sa.Column("narrative_schema", JSONB),
        sa.Column("page_pool_prompt", sa.Text),
        sa.Column("narrative_prompt", sa.Text),
        sa.Column("status", sa.String(20), server_default="drafting"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_theme_config_project_id", "theme_config", ["project_id"])

    # page_content
    op.create_table(
        "page_content",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("document_id", UUID(as_uuid=True), sa.ForeignKey("source_document.id", ondelete="CASCADE"), nullable=False),
        sa.Column("page_no", sa.Integer, nullable=False),
        sa.Column("raw_ocr_text", sa.Text),
        sa.Column("content", sa.Text),
        sa.Column("ocr_status", sa.String(20), server_default="pending"),
        sa.Column("ocr_provider", sa.String(50)),
        sa.Column("ocr_confidence", sa.Float),
        sa.Column("ocr_error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("document_id", "page_no"),
    )
    op.create_index("ix_page_content_document_id", "page_content", ["document_id"])

    # chat_session
    op.create_table(
        "chat_session",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("project.id", ondelete="CASCADE"), nullable=False),
        sa.Column("theme_id", UUID(as_uuid=True), sa.ForeignKey("theme_config.id", ondelete="SET NULL")),
        sa.Column("title", sa.String(200)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_chat_session_project_id", "chat_session", ["project_id"])
    op.create_index("ix_chat_session_theme_id", "chat_session", ["theme_id"])

    # chat_message
    op.create_table(
        "chat_message",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("session_id", UUID(as_uuid=True), sa.ForeignKey("chat_session.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("metadata", JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_chat_message_session_id", "chat_message", ["session_id"])

    # page_pool
    op.create_table(
        "page_pool",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("theme_id", UUID(as_uuid=True), sa.ForeignKey("theme_config.id", ondelete="CASCADE"), nullable=False),
        sa.Column("page_id", UUID(as_uuid=True), sa.ForeignKey("page_content.id", ondelete="CASCADE"), nullable=False),
        sa.Column("score", sa.Float),
        sa.Column("relevance_level", sa.String(20)),
        sa.Column("reason", sa.Text),
        sa.Column("generation", sa.Integer, nullable=False, server_default="1"),
        sa.Column("is_latest", sa.Boolean, server_default="true"),
        sa.Column("is_manual", sa.Boolean, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("theme_id", "page_id", "generation"),
    )
    op.create_index("ix_page_pool_theme_id", "page_pool", ["theme_id"])
    op.create_index("ix_page_pool_page_id", "page_pool", ["page_id"])
    op.create_index("ix_page_pool_theme_latest", "page_pool", ["theme_id", "is_latest"])

    # narrative_unit
    op.create_table(
        "narrative_unit",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("theme_id", UUID(as_uuid=True), sa.ForeignKey("theme_config.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_page", sa.Integer),
        sa.Column("fields", JSONB, nullable=False),
        sa.Column("confidence", sa.Float),
        sa.Column("generation", sa.Integer, nullable=False, server_default="1"),
        sa.Column("is_latest", sa.Boolean, server_default="true"),
        sa.Column("is_manual", sa.Boolean, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_narrative_unit_theme_id", "narrative_unit", ["theme_id"])
    op.create_index("ix_narrative_unit_theme_latest", "narrative_unit", ["theme_id", "is_latest"])

    # task
    op.create_table(
        "task",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("project.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_type", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), server_default="pending"),
        sa.Column("progress", sa.Integer, server_default="0"),
        sa.Column("result", JSONB),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_task_project_id", "task", ["project_id"])
    op.create_index("ix_task_status", "task", ["status"])
    op.create_index("ix_task_project_status", "task", ["project_id", "status"])


def downgrade() -> None:
    op.drop_table("task")
    op.drop_table("narrative_unit")
    op.drop_table("page_pool")
    op.drop_table("chat_message")
    op.drop_table("chat_session")
    op.drop_table("page_content")
    op.drop_table("theme_config")
    op.drop_table("source_document")
    op.drop_table("project")
