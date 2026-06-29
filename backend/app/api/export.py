import io
import urllib.parse
import uuid
import zipfile

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.models.theme import ThemeConfig
from app.services.export_service import (
    export_base_table,
    export_classification,
    export_document_base_table,
    export_page_pool,
    export_narrative_units,
)

router = APIRouter(prefix="/api", tags=["导出"])


@router.get("/projects/{project_id}/export/base-table")
async def export_project_base_table(
    project_id: uuid.UUID,
    format: str = Query("excel", description="导出格式: excel/csv/json"),
    db: AsyncSession = Depends(get_db),
):
    """导出 OCR/页面导入后的底表。"""
    if format not in ("excel", "csv", "json"):
        raise HTTPException(status_code=400, detail=f"不支持的格式: {format}")

    try:
        path = await export_base_table(db, project_id, format)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return FileResponse(path, filename=path.name)


@router.get("/documents/{document_id}/export/base-table")
async def export_document_base_table_data(
    document_id: uuid.UUID,
    format: str = Query("excel", description="导出格式: excel/csv/json"),
    db: AsyncSession = Depends(get_db),
):
    """导出单个上传文件 OCR/页面导入后的底表。"""
    if format not in ("excel", "csv", "json"):
        raise HTTPException(status_code=400, detail=f"不支持的格式: {format}")

    try:
        path = await export_document_base_table(db, document_id, format)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return FileResponse(path, filename=path.name)


@router.get("/projects/{project_id}/export/classification")
async def export_classification_data(
    project_id: uuid.UUID,
    format: str = Query("excel", description="导出格式: excel/csv/json"),
    db: AsyncSession = Depends(get_db),
):
    """导出页级分类结果。"""
    if format not in ("excel", "csv", "json"):
        raise HTTPException(status_code=400, detail=f"不支持的格式: {format}")

    try:
        path = await export_classification(db, project_id, format)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return FileResponse(path, filename=path.name)


@router.get("/themes/{theme_id}/export")
async def export_data(
    theme_id: uuid.UUID,
    type: str = Query("page_pool", description="导出类型: page_pool/narrative/all"),
    format: str = Query("excel", description="导出格式: excel/csv/json"),
    db: AsyncSession = Depends(get_db),
):
    """导出专题数据。"""
    if format not in ("excel", "csv", "json"):
        raise HTTPException(status_code=400, detail=f"不支持的格式: {format}")
    if type not in ("page_pool", "narrative", "all"):
        raise HTTPException(status_code=400, detail=f"无效的导出类型: {type}")

    try:
        if type == "page_pool":
            path = await export_page_pool(db, theme_id, format)
            return FileResponse(path, filename=path.name)

        if type == "narrative":
            path = await export_narrative_units(db, theme_id, format)
            return FileResponse(path, filename=path.name)

        files = []
        missing_parts = []
        for label, exporter in (
            ("页面池", export_page_pool),
            ("叙事单元", export_narrative_units),
        ):
            try:
                files.append(await exporter(db, theme_id, format))
            except ValueError as e:
                missing_parts.append(f"{label}: {e}")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not files:
        detail = "没有可导出的专题数据"
        if missing_parts:
            detail += "：" + "；".join(missing_parts)
        raise HTTPException(status_code=400, detail=detail)

    theme_result = await db.execute(select(ThemeConfig).where(ThemeConfig.id == theme_id))
    theme = theme_result.scalar_one_or_none()
    zip_name = f"{theme.theme if theme else theme_id}_全部结果.zip"
    encoded_zip_name = urllib.parse.quote(zip_name)

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, f.name)
    zip_buffer.seek(0)

    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_zip_name}"},
    )
