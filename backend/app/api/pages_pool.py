import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, owned_theme, owned_pool_entry, owned_page
from app.models.page import PageContent, PagePool
from app.models.theme import ThemeConfig
from app.models.platform import User
from app.services.auth_service import get_current_user
from app.schemas.page import PagePoolCreate, PagePoolResponse, PagePoolUpdate
from app.services.task_manager import task_manager
from app.services.page_pool_service import (
    MAX_THEMES_PER_BATCH,
    run_multi_theme_page_pool_generation,
    run_page_pool_generation,
)

router = APIRouter(prefix="/api", tags=["page-pool"])


@router.post("/themes/{theme_id}/page-pool/generate")
async def generate_page_pool(
    theme_id: uuid.UUID,
    payload: dict = Body(default_factory=dict),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """触发页面池生成任务"""
    theme = await owned_theme(theme_id, user, db)

    task_id = await task_manager.submit(
        task_type="page_pool",
        project_id=theme.project_id,
        coro_func=run_page_pool_generation,
        quote_id=payload.get("quote_id"),
        theme_id=str(theme_id),
        llm_config=None,
        llm_concurrency=None,
    )

    return {"task_id": str(task_id), "status": "submitted"}


@router.post("/themes/page-pool/generate-batch")
async def generate_page_pool_batch(
    payload: dict = Body(default_factory=dict),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """触发多专题联合页面池生成任务"""
    raw_theme_ids = payload.get("theme_ids") or []
    if not isinstance(raw_theme_ids, list) or not raw_theme_ids:
        raise HTTPException(status_code=400, detail="请传入 theme_ids")

    theme_ids: list[uuid.UUID] = []
    try:
        for raw_id in raw_theme_ids:
            theme_id = uuid.UUID(str(raw_id))
            if theme_id not in theme_ids:
                theme_ids.append(theme_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="theme_ids 中存在无效专题 ID")

    if len(theme_ids) > MAX_THEMES_PER_BATCH:
        raise HTTPException(status_code=400, detail=f"一次最多联合评估 {MAX_THEMES_PER_BATCH} 个专题")

    result = await db.execute(select(ThemeConfig).where(ThemeConfig.id.in_(theme_ids)))
    themes = result.scalars().all()
    themes_by_id = {theme.id: theme for theme in themes}
    missing = [str(theme_id) for theme_id in theme_ids if theme_id not in themes_by_id]
    if missing:
        raise HTTPException(status_code=404, detail=f"专题不存在: {', '.join(missing)}")

    for theme in themes:
        await owned_theme(theme.id, user, db)

    project_ids = {theme.project_id for theme in themes}
    if len(project_ids) != 1:
        raise HTTPException(status_code=400, detail="所有专题必须属于同一个项目")
    project_id = next(iter(project_ids))

    task_id = await task_manager.submit(
        task_type="page_pool_batch",
        project_id=project_id,
        coro_func=run_multi_theme_page_pool_generation,
        theme_ids=[str(theme_id) for theme_id in theme_ids],
        quote_id=payload.get("quote_id"),
    )

    return {"task_id": str(task_id), "status": "submitted"}


@router.get("/themes/{theme_id}/page-pool", response_model=list[PagePoolResponse])
async def list_page_pool(
    theme_id: uuid.UUID,
    generation: int | None = Query(None, description="指定版本，不传则返回最新"),
    relevance_level: str | None = Query(None, description="筛选标签: core/borderline/excluded"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await owned_theme(theme_id, user, db)
    query = select(PagePool).where(PagePool.theme_id == theme_id)

    if generation is not None:
        query = query.where(PagePool.generation == generation)
    else:
        query = query.where(PagePool.is_latest == True)

    if relevance_level:
        query = query.where(PagePool.relevance_level == relevance_level)

    query = query.order_by(PagePool.score.desc())
    result = await db.execute(query)
    return result.scalars().all()


@router.post("/themes/{theme_id}/page-pool", response_model=PagePoolResponse, status_code=201)
async def add_page_to_pool(
    theme_id: uuid.UUID,
    data: PagePoolCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """手动添加页面到页面池"""
    from sqlalchemy import func
    from app.models.theme import ThemeConfig
    from app.models.page import PageContent
    from app.models.project import SourceDocument

    theme_obj = await owned_theme(theme_id, user, db)

    page_obj = await owned_page(data.page_id, user, db)

    # BE-011: 校验 page 属于同一 project
    doc = await db.execute(select(SourceDocument).where(SourceDocument.id == page_obj.document_id))
    doc_obj = doc.scalar_one_or_none()
    if doc_obj is None or doc_obj.project_id != theme_obj.project_id:
        raise HTTPException(status_code=400, detail="页面不属于当前项目")

    # 获取当前最大 generation
    gen_result = await db.execute(
        select(func.max(PagePool.generation)).where(PagePool.theme_id == theme_id)
    )
    current_gen = gen_result.scalar() or 0

    entry = PagePool(
        theme_id=theme_id,
        page_id=data.page_id,
        score=data.score or 1.0,
        relevance_level=data.relevance_level or "core",
        reason=data.reason or "手动添加",
        generation=current_gen or 1,
        is_latest=True,
        is_manual=True,
    )
    db.add(entry)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=400, detail="该页面已在当前版本的页面池中")
    await db.refresh(entry)
    return entry


@router.patch("/page-pool/{entry_id}", response_model=PagePoolResponse)
async def update_page_pool_entry(
    entry_id: uuid.UUID,
    data: PagePoolUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    entry = await owned_pool_entry(entry_id, user, db)

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(entry, field, value)

    # 自动更新 relevance_level
    if entry.score is not None:
        from app.config import settings
        if entry.score >= settings.score_core_threshold:
            entry.relevance_level = "core"
        elif entry.score >= settings.score_borderline_threshold:
            entry.relevance_level = "borderline"
        else:
            entry.relevance_level = "excluded"

    await db.commit()
    await db.refresh(entry)
    return entry


@router.delete("/page-pool/{entry_id}", status_code=204)
async def delete_page_pool_entry(
    entry_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    entry = await owned_pool_entry(entry_id, user, db)
    await db.delete(entry)
    await db.commit()
