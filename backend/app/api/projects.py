import logging
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.models.project import Project
from app.models.project import SourceDocument
from app.models.task import Task
from app.models.theme import ThemeConfig
from app.schemas.project import ProjectCreate, ProjectResponse, ProjectUpdate
from app.services.task_manager import task_manager
from app.utils.file_storage import get_input_dir, get_export_dir, get_page_images_dir

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.post("", response_model=ProjectResponse, status_code=201)
async def create_project(data: ProjectCreate, db: AsyncSession = Depends(get_db)):
    project = Project(name=data.name, description=data.description)
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


@router.get("", response_model=list[ProjectResponse])
async def list_projects(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Project).order_by(Project.created_at.desc()))
    return result.scalars().all()


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(project_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.delete("/{project_id}", status_code=204)
async def delete_project(project_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    # BE-FIX-020: 取消该项目的所有运行中/等待中的后台任务
    task_rows = await db.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.status.in_(["running", "pending"]),
        )
    )
    for task_row in task_rows.scalars().all():
        cancelled = await task_manager.cancel(task_row.id)
        if not cancelled:
            logger.warning(f"无法取消任务 {task_row.id}（可能已完成）")

    # BE-FIX-011: 删除数据库前先收集所有待清理的文件路径
    dirs_to_clean: list[Path] = []
    files_to_clean: list[Path] = []

    # 项目输入目录
    input_dir = get_input_dir(project_id)
    if input_dir.exists():
        dirs_to_clean.append(input_dir)

    # 所有文档的页面图片目录
    docs_result = await db.execute(
        select(SourceDocument.id).where(SourceDocument.project_id == project_id)
    )
    for (doc_id,) in docs_result.fetchall():
        img_dir = get_page_images_dir(doc_id)
        if img_dir.exists():
            dirs_to_clean.append(img_dir)

    # 所有专题的导出目录
    themes_result = await db.execute(
        select(ThemeConfig.id).where(ThemeConfig.project_id == project_id)
    )
    for (theme_id,) in themes_result.fetchall():
        export_dir = get_export_dir(theme_id)
        if export_dir.exists():
            dirs_to_clean.append(export_dir)

    # BE-FIX-005: 先删除数据库记录
    await db.delete(project)
    await db.commit()

    # BE-FIX-011: 数据库提交成功后再清理物理文件
    for d in dirs_to_clean:
        try:
            shutil.rmtree(d)
        except Exception as e:
            logger.warning(f"清理目录失败 {d}: {e}")
    for f in files_to_clean:
        try:
            f.unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"清理文件失败 {f}: {e}")
