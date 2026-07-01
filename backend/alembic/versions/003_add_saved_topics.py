"""add saved_topics to source_document

Revision ID: 003
Revises: 002
Create Date: 2025-07-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("source_document", sa.Column("saved_topics", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("source_document", "saved_topics")
