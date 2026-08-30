import asyncio
import sys

import sqlalchemy as sa
from app.config import settings
from app.database import Base, engine
import app.models.chat  # noqa: F401
import app.models.narrative  # noqa: F401
import app.models.page  # noqa: F401
import app.models.project  # noqa: F401
import app.models.task  # noqa: F401
import app.models.theme  # noqa: F401
import app.models.platform  # noqa: F401
from sqlalchemy.dialects.postgresql import JSONB, UUID


async def main() -> None:
    print(f"database_url={settings.database_url}")
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = sa.JSON()
            elif isinstance(column.type, UUID):
                column.type = sa.Uuid()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    print("sqlite schema created")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

