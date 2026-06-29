import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.models.narrative import NarrativeUnit
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
    db: AsyncSession = Depends(get_db),
):
    """触发叙事单元抽取任务"""
    result = await db.execute(select(ThemeConfig).where(ThemeConfig.id == theme_id))
    theme = result.scalar_one_or_none()
    if theme is None:
        raise HTTPException(status_code=404, detail="专题不存在")

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
        llm_config=payload.get("llm_config"),
        llm_concurrency=payload.get("llm_concurrency", 5),
    )

    return {"task_id": str(task_id), "status": "submitted"}


@router.get("/themes/{theme_id}/narratives", response_model=list[NarrativeUnitResponse])
async def list_narratives(
    theme_id: uuid.UUID,
    generation: int | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
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
    db: AsyncSession = Depends(get_db),
):
    """手动添加叙事单元"""
    from sqlalchemy import func

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
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(NarrativeUnit).where(NarrativeUnit.id == unit_id))
    unit = result.scalar_one_or_none()
    if unit is None:
        raise HTTPException(status_code=404, detail="Narrative unit not found")

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(unit, field, value)

    await db.commit()
    await db.refresh(unit)
    return unit


@router.delete("/narratives/{unit_id}", status_code=204)
async def delete_narrative(unit_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(NarrativeUnit).where(NarrativeUnit.id == unit_id))
    unit = result.scalar_one_or_none()
    if unit is None:
        raise HTTPException(status_code=404, detail="Narrative unit not found")
    await db.delete(unit)
    await db.commit()
