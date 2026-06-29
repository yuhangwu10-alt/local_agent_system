import logging
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import SourceDocument

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".pdf", ".xlsx"}


async def scan_input_directory(
    input_dir: Path,
    project_id: uuid.UUID,
    db: AsyncSession,
) -> list[SourceDocument]:
    """扫描 input/ 目录，注册未导入的文件"""
    if not input_dir.exists():
        return []

    # 获取已注册的文件路径
    result = await db.execute(
        select(SourceDocument.file_path).where(SourceDocument.project_id == project_id)
    )
    existing_paths = {row[0] for row in result.fetchall()}

    new_files = []
    for file_path in sorted(input_dir.iterdir()):
        if file_path.is_file() and file_path.suffix.lower() in SUPPORTED_EXTENSIONS:
            relative_path = str(file_path.relative_to(input_dir))
            if relative_path not in existing_paths:
                file_type = "pdf" if file_path.suffix.lower() == ".pdf" else "excel"
                doc = SourceDocument(
                    project_id=project_id,
                    file_type=file_type,
                    file_path=relative_path,
                    file_name=file_path.name,
                    status="registered",
                )
                db.add(doc)
                new_files.append(doc)
                logger.info(f"Discovered new file: {file_path.name}")

    if new_files:
        await db.commit()
        for doc in new_files:
            await db.refresh(doc)

    return new_files
