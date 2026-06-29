import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.models.page import PageContent
from app.schemas.page import PageContentResponse, PageContentUpdate

router = APIRouter(prefix="/api", tags=["pages"])


@router.get("/documents/{document_id}/pages", response_model=list[PageContentResponse])
async def list_pages(
    document_id: uuid.UUID,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(PageContent)
        .where(PageContent.document_id == document_id)
        .order_by(PageContent.page_no)
        .offset(skip)
        .limit(limit)
    )
    return result.scalars().all()


@router.patch("/pages/{page_id}", response_model=PageContentResponse)
async def update_page(page_id: uuid.UUID, data: PageContentUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PageContent).where(PageContent.id == page_id))
    page = result.scalar_one_or_none()
    if page is None:
        raise HTTPException(status_code=404, detail="Page not found")

    page.content = data.content
    await db.commit()
    await db.refresh(page)
    return page
