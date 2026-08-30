"""commercial platform tables

Revision ID: 005
Revises: 004
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
JSONB = sa.JSON

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "app_user",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("role", sa.String(20), nullable=False, server_default="user"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("email", name="uq_app_user_email"),
    )
    op.create_index("ix_app_user_email", "app_user", ["email"])
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("project", recreate="always") as batch:
            batch.add_column(sa.Column("user_id", UUID(as_uuid=True), nullable=True))
            batch.create_foreign_key("fk_project_user_id", "app_user", ["user_id"], ["id"], ondelete="SET NULL")
        op.create_index("ix_project_user_id", "project", ["user_id"])
        with op.batch_alter_table("source_document", recreate="always") as batch:
            batch.add_column(sa.Column("user_id", UUID(as_uuid=True), nullable=True))
            batch.create_foreign_key("fk_source_document_user_id", "app_user", ["user_id"], ["id"], ondelete="SET NULL")
        op.create_index("ix_source_document_user_id", "source_document", ["user_id"])
    else:
        op.add_column("project", sa.Column("user_id", UUID(as_uuid=True), nullable=True))
        op.create_foreign_key("fk_project_user_id", "project", "app_user", ["user_id"], ["id"], ondelete="SET NULL")
        op.create_index("ix_project_user_id", "project", ["user_id"])
        op.add_column("source_document", sa.Column("user_id", UUID(as_uuid=True), nullable=True))
        op.create_foreign_key("fk_source_document_user_id", "source_document", "app_user", ["user_id"], ["id"], ondelete="SET NULL")
        op.create_index("ix_source_document_user_id", "source_document", ["user_id"])
    op.create_table(
        "wallet",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("balance", sa.Float(), nullable=False, server_default="0"),
        sa.Column("frozen", sa.Float(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", name="uq_wallet_user_id"),
    )
    op.create_table(
        "ledger_entry",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("balance_after", sa.Float(), nullable=False),
        sa.Column("entry_type", sa.String(30), nullable=False),
        sa.Column("reference_id", sa.String(120)),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_ledger_entry_user_id", "ledger_entry", ["user_id"])
    op.create_index("ix_ledger_entry_reference_id", "ledger_entry", ["reference_id"])
    op.create_table(
        "redeem_code",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("code_hash", sa.String(128), nullable=False),
        sa.Column("code_hint", sa.String(20), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("batch_name", sa.String(120)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("redeemed_by", UUID(as_uuid=True)),
        sa.Column("redeemed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["redeemed_by"], ["app_user.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("code_hash", name="uq_redeem_code_hash"),
    )
    op.create_index("ix_redeem_code_code_hash", "redeem_code", ["code_hash"])
    op.create_table(
        "model_profile",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("provider", sa.String(60), nullable=False),
        sa.Column("base_url", sa.String(500), nullable=False),
        sa.Column("api_key", sa.Text(), nullable=False),
        sa.Column("model", sa.String(160), nullable=False),
        sa.Column("stages", JSONB),
        sa.Column("max_concurrency", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("retries", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("name", name="uq_model_profile_name"),
    )
    op.create_table(
        "billing_quote",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", UUID(as_uuid=True)),
        sa.Column("units", sa.Float(), nullable=False),
        sa.Column("unit_type", sa.String(20), nullable=False),
        sa.Column("unit_price", sa.Float(), nullable=False),
        sa.Column("total", sa.Float(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="quoted"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["task.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_billing_quote_user_id", "billing_quote", ["user_id"])
    op.create_index("ix_billing_quote_project_id", "billing_quote", ["project_id"])
    op.create_index("ix_billing_quote_task_id", "billing_quote", ["task_id"])


def downgrade() -> None:
    op.drop_table("billing_quote")
    op.drop_table("model_profile")
    op.drop_table("redeem_code")
    op.drop_table("ledger_entry")
    op.drop_table("wallet")
    op.drop_index("ix_source_document_user_id", table_name="source_document")
    op.drop_constraint("fk_source_document_user_id", "source_document", type_="foreignkey")
    op.drop_column("source_document", "user_id")
    op.drop_index("ix_project_user_id", table_name="project")
    op.drop_constraint("fk_project_user_id", "project", type_="foreignkey")
    op.drop_column("project", "user_id")
    op.drop_index("ix_app_user_email", table_name="app_user")
    op.drop_table("app_user")




