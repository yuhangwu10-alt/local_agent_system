"""Make the legacy direct-created SQLite development database compatible.

Docker/production databases use Alembic. Older local_dev.db files were made
with metadata.create_all(), so they do not have an alembic_version row and
cannot be upgraded in place by the migration chain. Keep their data and add
the small set of task columns introduced by the platform work.
"""

import os
import sqlite3
from pathlib import Path


def database_path() -> Path:
    url = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./local_dev.db")
    if not url.startswith("sqlite"):
        raise SystemExit("prepare_sqlite_dev.py only supports SQLite DATABASE_URL")
    raw = url.split("///", 1)[-1].split("?", 1)[0]
    path = Path(raw)
    return path if path.is_absolute() else Path.cwd() / path


def main() -> None:
    path = database_path()
    if not path.exists():
        return
    with sqlite3.connect(path) as db:
        tables = {row[0] for row in db.execute("select name from sqlite_master where type='table'")}
        table_additions = {
            "project": {"user_id": "CHAR(32)"},
            "source_document": {"user_id": "CHAR(32)"},
            "task": {
                "user_id": "CHAR(32)",
                "billing_quote_id": "CHAR(32)",
                "charge_status": "VARCHAR(20) NOT NULL DEFAULT 'none'",
                "attempt_count": "INTEGER NOT NULL DEFAULT 0",
                "heartbeat_at": "DATETIME",
                "worker_id": "VARCHAR(120)",
                "lease_expires_at": "DATETIME",
            },
            "billing_quote": {
                "document_ids": "JSON",
                "expires_at": "DATETIME",
            },
        }
        for table, additions in table_additions.items():
            if table not in tables:
                continue
            columns = {row[1] for row in db.execute(f"pragma table_info({table})")}
            for name, column_type in additions.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {column_type}")
        if "billing_quote" in tables:
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_billing_quote_task_id "
                "ON billing_quote(task_id) WHERE task_id IS NOT NULL"
            )
        db.commit()


if __name__ == "__main__":
    main()
