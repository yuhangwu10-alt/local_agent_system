from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from fastapi import Depends, HTTPException

from app.database import async_session
from app.models.platform import User
from app.models.project import Project, SourceDocument
from app.models.theme import ThemeConfig
from app.models.page import PageContent, PagePool
from app.models.task import Task
from app.models.chat import ChatSession


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def _owner_filter(query, user: User, owner_column):
    return query if user.role == "admin" else query.where(owner_column == user.id)


async def owned_project(project_id, user: User, db: AsyncSession) -> Project:
    query = select(Project).where(Project.id == project_id)
    query = _owner_filter(query, user, Project.user_id)
    project = await db.scalar(query)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在或无权访问")
    return project


async def owned_document(document_id, user: User, db: AsyncSession) -> SourceDocument:
    query = select(SourceDocument).join(Project, SourceDocument.project_id == Project.id).where(SourceDocument.id == document_id)
    query = _owner_filter(query, user, Project.user_id)
    document = await db.scalar(query)
    if document is None:
        raise HTTPException(status_code=404, detail="文档不存在或无权访问")
    return document


async def owned_theme(theme_id, user: User, db: AsyncSession) -> ThemeConfig:
    query = select(ThemeConfig).join(Project, ThemeConfig.project_id == Project.id).where(ThemeConfig.id == theme_id)
    query = _owner_filter(query, user, Project.user_id)
    theme = await db.scalar(query)
    if theme is None:
        raise HTTPException(status_code=404, detail="专题不存在或无权访问")
    return theme


async def owned_page(page_id, user: User, db: AsyncSession) -> PageContent:
    query = select(PageContent).join(SourceDocument, PageContent.document_id == SourceDocument.id).join(Project, SourceDocument.project_id == Project.id).where(PageContent.id == page_id)
    query = _owner_filter(query, user, Project.user_id)
    page = await db.scalar(query)
    if page is None:
        raise HTTPException(status_code=404, detail="页面不存在或无权访问")
    return page


async def owned_task(task_id, user: User, db: AsyncSession) -> Task:
    query = select(Task).join(Project, Task.project_id == Project.id).where(Task.id == task_id)
    query = _owner_filter(query, user, Project.user_id)
    task = await db.scalar(query)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在或无权访问")
    return task


async def owned_session(session_id, user: User, db: AsyncSession) -> ChatSession:
    query = select(ChatSession).join(Project, ChatSession.project_id == Project.id).where(ChatSession.id == session_id)
    query = _owner_filter(query, user, Project.user_id)
    session = await db.scalar(query)
    if session is None:
        raise HTTPException(status_code=404, detail="对话不存在或无权访问")
    return session

async def owned_narrative(unit_id, user: User, db: AsyncSession):
    from app.models.narrative import NarrativeUnit
    query = select(NarrativeUnit).join(ThemeConfig, NarrativeUnit.theme_id == ThemeConfig.id).join(Project, ThemeConfig.project_id == Project.id).where(NarrativeUnit.id == unit_id)
    query = _owner_filter(query, user, Project.user_id)
    unit = await db.scalar(query)
    if unit is None:
        raise HTTPException(status_code=404, detail="叙事单元不存在或无权访问")
    return unit


async def owned_pool_entry(entry_id, user: User, db: AsyncSession):
    query = select(PagePool).join(ThemeConfig, PagePool.theme_id == ThemeConfig.id).join(Project, ThemeConfig.project_id == Project.id).where(PagePool.id == entry_id)
    query = _owner_filter(query, user, Project.user_id)
    entry = await db.scalar(query)
    if entry is None:
        raise HTTPException(status_code=404, detail="页面池条目不存在或无权访问")
    return entry