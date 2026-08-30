import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, owned_theme, owned_narrative
from app.models.narrative import NarrativeUnit
from app.models.platform import User
from app.services.auth_service import get_current_user
from app.models.page import PagePool
from app.models.theme import ThemeConfig
from app.schemas.narrative import NarrativeUnitCreate, NarrativeUnitResponse, NarrativeUnitUpdate
from app.services.task_manager import task_manager
from app.services.narrative_service import run_narrative_extraction

router = APIRouter(prefix="/api", tags=["narratives"])


@router.post("/themes/{theme_id}/narratives/generate")
async def generate_narratives(
    theme_id: uuid.UUID,
    payload: dict = Body(default_factory=dict),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """触发叙事单元抽取任务"""
    theme = await owned_theme(theme_id, user, db)

    # 举一反三: 检查是否有页面池数据
    pool_count = await db.execute(
        select(func.count(PagePool.id))
        .where(PagePool.theme_id == theme_id, PagePool.is_latest == True)
    )
    if pool_count.scalar() == 0:
        raise HTTPException(status_code=400, detail="请先生成页面池，再抽取叙事单元")

    task_id = await task_manager.submit(
        task_type="narrative",
        project_id=theme.project_id,
        coro_func=run_narrative_extraction,
        theme_id=str(theme_id),
        quote_id=payload.get("quote_id"),
    )

    return {"task_id": str(task_id), "status": "submitted"}


@router.get("/themes/{theme_id}/narratives", response_model=list[NarrativeUnitResponse])
async def list_narratives(
    theme_id: uuid.UUID,
    generation: int | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await owned_theme(theme_id, user, db)
    query = select(NarrativeUnit).where(NarrativeUnit.theme_id == theme_id)

    if generation is not None:
        query = query.where(NarrativeUnit.generation == generation)
    else:
        query = query.where(NarrativeUnit.is_latest == True)

    query = query.order_by(NarrativeUnit.source_page)
    result = await db.execute(query)
    return result.scalars().all()


@router.post("/themes/{theme_id}/narratives", response_model=NarrativeUnitResponse, status_code=201)
async def add_narrative(
    theme_id: uuid.UUID,
    data: NarrativeUnitCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """手动添加叙事单元"""
    await owned_theme(theme_id, user, db)

    gen_result = await db.execute(
        select(func.max(NarrativeUnit.generation)).where(NarrativeUnit.theme_id == theme_id)
    )
    current_gen = gen_result.scalar() or 0

    unit = NarrativeUnit(
        theme_id=theme_id,
        source_page=data.source_page,
        fields=data.fields,
        confidence=data.confidence,
        generation=current_gen or 1,
        is_latest=True,
        is_manual=True,
    )
    db.add(unit)
    await db.commit()
    await db.refresh(unit)
    return unit


@router.patch("/narratives/{unit_id}", response_model=NarrativeUnitResponse)
async def update_narrative(
    unit_id: uuid.UUID,
    data: NarrativeUnitUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    unit = await owned_narrative(unit_id, user, db)

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(unit, field, value)

    await db.commit()
    await db.refresh(unit)
    return unit


@router.delete("/narratives/{unit_id}", status_code=204)
async def delete_narrative(unit_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    unit = await owned_narrative(unit_id, user, db)
    await db.delete(unit)
    await db.commit()
