"""add task worker lease fields"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("task", recreate="always") as batch:
            batch.add_column(sa.Column("worker_id", sa.String(120), nullable=True))
            batch.add_column(sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    else:
        op.add_column("task", sa.Column("worker_id", sa.String(120), nullable=True))
        op.add_column("task", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_task_worker_id", "task", ["worker_id"])
    op.create_index("ix_task_lease_expires_at", "task", ["lease_expires_at"])


def downgrade() -> None:
    op.drop_index("ix_task_lease_expires_at", table_name="task")
    op.drop_index("ix_task_worker_id", table_name="task")
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("task", recreate="always") as batch:
            batch.drop_column("lease_expires_at")
            batch.drop_column("worker_id")
    else:
        op.drop_column("lease_expires_at", "task")
        op.drop_column("worker_id", "task")
