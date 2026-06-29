"""add classification and pool fields

Revision ID: 002
Revises: 001
Create Date: 2025-05-31
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # page_content 新增 classification 字段
    op.add_column("page_content", sa.Column("classification", JSONB, nullable=True))

    # page_pool 新增 5 个字段
    op.add_column("page_pool", sa.Column("命中关键词", JSONB, nullable=True))
    op.add_column("page_pool", sa.Column("命中规则", JSONB, nullable=True))
    op.add_column("page_pool", sa.Column("关键词命中数", sa.Integer, nullable=True))
    op.add_column("page_pool", sa.Column("信号命中数", sa.Integer, nullable=True))
    op.add_column("page_pool", sa.Column("置信度", sa.String(10), nullable=True))


def downgrade() -> None:
    op.drop_column("page_pool", "置信度")
    op.drop_column("page_pool", "信号命中数")
    op.drop_column("page_pool", "关键词命中数")
    op.drop_column("page_pool", "命中规则")
    op.drop_column("page_pool", "命中关键词")
    op.drop_column("page_content", "classification")
