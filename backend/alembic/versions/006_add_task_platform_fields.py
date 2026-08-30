"""add ownership and billing fields to tasks"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("task", recreate="always") as batch:
            batch.add_column(sa.Column("user_id", UUID(as_uuid=True), nullable=True))
            batch.add_column(sa.Column("billing_quote_id", UUID(as_uuid=True), nullable=True))
            batch.add_column(sa.Column("charge_status", sa.String(20), nullable=False, server_default="none"))
            batch.add_column(sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"))
            batch.add_column(sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
            batch.create_foreign_key("fk_task_user_id", "app_user", ["user_id"], ["id"], ondelete="SET NULL")
            batch.create_foreign_key("fk_task_billing_quote_id", "billing_quote", ["billing_quote_id"], ["id"], ondelete="SET NULL")
    else:
        op.add_column("task", sa.Column("user_id", UUID(as_uuid=True), nullable=True))
        op.add_column("task", sa.Column("billing_quote_id", UUID(as_uuid=True), nullable=True))
        op.add_column("task", sa.Column("charge_status", sa.String(20), nullable=False, server_default="none"))
        op.add_column("task", sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"))
        op.add_column("task", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
        op.create_foreign_key("fk_task_user_id", "task", "app_user", ["user_id"], ["id"], ondelete="SET NULL")
        op.create_foreign_key("fk_task_billing_quote_id", "task", "billing_quote", ["billing_quote_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_task_user_id", "task", ["user_id"])
    op.create_index("ix_task_billing_quote_id", "task", ["billing_quote_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("task", recreate="always") as batch:
            batch.drop_constraint("fk_task_billing_quote_id", type_="foreignkey")
            batch.drop_constraint("fk_task_user_id", type_="foreignkey")
            batch.drop_column("heartbeat_at")
            batch.drop_column("attempt_count")
            batch.drop_column("charge_status")
            batch.drop_column("billing_quote_id")
            batch.drop_column("user_id")
    else:
        op.drop_constraint("fk_task_billing_quote_id", "task", type_="foreignkey")
        op.drop_constraint("fk_task_user_id", "task", type_="foreignkey")
        op.drop_column("heartbeat_at", "task")
        op.drop_column("attempt_count", "task")
        op.drop_column("charge_status", "task")
        op.drop_column("billing_quote_id", "task")
        op.drop_column("user_id", "task")
    op.drop_index("ix_task_billing_quote_id", table_name="task")
    op.drop_index("ix_task_user_id", table_name="task")