"""add payload to task

Revision ID: 004
Revises: 003
Create Date: 2026-07-04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("task", sa.Column("payload", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("task", "payload")
