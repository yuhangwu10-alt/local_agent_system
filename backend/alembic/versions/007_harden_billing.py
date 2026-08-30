"""harden billing precision, quote snapshots and idempotent task binding

Revision ID: 007
Revises: 006
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    numeric_money = sa.Numeric(12, 2)
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("wallet", recreate="always") as batch:
            batch.alter_column("balance", type_=numeric_money)
            batch.alter_column("frozen", type_=numeric_money)
        with op.batch_alter_table("ledger_entry", recreate="always") as batch:
            batch.alter_column("amount", type_=numeric_money)
            batch.alter_column("balance_after", type_=numeric_money)
        with op.batch_alter_table("redeem_code", recreate="always") as batch:
            batch.alter_column("amount", type_=numeric_money)
        with op.batch_alter_table("billing_quote", recreate="always") as batch:
            batch.alter_column("units", type_=numeric_money)
            batch.alter_column("unit_price", type_=sa.Numeric(12, 4))
            batch.alter_column("total", type_=numeric_money)
            batch.add_column(sa.Column("document_ids", sa.JSON(), nullable=True))
            batch.add_column(sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    else:
        op.alter_column("wallet", "balance", type_=numeric_money)
        op.alter_column("wallet", "frozen", type_=numeric_money)
        op.alter_column("ledger_entry", "amount", type_=numeric_money)
        op.alter_column("ledger_entry", "balance_after", type_=numeric_money)
        op.alter_column("redeem_code", "amount", type_=numeric_money)
        op.alter_column("billing_quote", "units", type_=numeric_money)
        op.alter_column("billing_quote", "unit_price", type_=sa.Numeric(12, 4))
        op.alter_column("billing_quote", "total", type_=numeric_money)
        op.add_column("billing_quote", sa.Column("document_ids", sa.JSON(), nullable=True))
        op.add_column("billing_quote", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("billing_quote", recreate="always") as batch:
            batch.create_unique_constraint("uq_billing_quote_task_id", ["task_id"])
    else:
        op.create_unique_constraint("uq_billing_quote_task_id", "billing_quote", ["task_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("billing_quote", recreate="always") as batch:
            batch.drop_constraint("uq_billing_quote_task_id", type_="unique")
            batch.drop_column("expires_at")
            batch.drop_column("document_ids")
    else:
        op.drop_constraint("uq_billing_quote_task_id", "billing_quote", type_="unique")
        op.drop_column("billing_quote", "expires_at")
        op.drop_column("billing_quote", "document_ids")
