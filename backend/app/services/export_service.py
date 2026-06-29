import logging
import re
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.page import PageContent, PagePool
from app.models.narrative import NarrativeUnit
from app.models.theme import ThemeConfig
from app.models.project import SourceDocument
from app.services.excel_service import export_to_excel, export_to_csv, export_to_json
from app.utils.file_storage import get_export_dir, sanitize_filename, ensure_dir

logger = logging.getLogger(__name__)

GENERIC_NARRATIVE_EXPORT_FIELDS = [
    "专题名称",
    "叙事单元标题",
    "单元类型",
    "涉及对象",
    "时间线索",
    "地点线索",
    "事件或行为",
    "关键词命中",
    "原文证据",
]


def _sanitize_sheet_name(name: str) -> str:
    """BE-013: 清理 Excel sheet 名，截断到 31 字符，替换非法字符"""
    # 替换 Excel 不允许的字符
    cleaned = re.sub(r'[\[\]:*?/\\]', '_', name)
    # 截断到 31 字符
    return cleaned[:31] if cleaned else "Sheet1"


async def export_classification(
    db: AsyncSession,
    project_id: uuid.UUID,
    format: str = "excel",
) -> Path:
    """导出页级分类结果"""
    result = await db.execute(
        select(PageContent, SourceDocument)
        .join(SourceDocument, PageContent.document_id == SourceDocument.id)
        .where(
            SourceDocument.project_id == project_id,
            PageContent.classification.isnot(None),  # BE-FIX-010: 只导出已分类的页面
        )
        .order_by(PageContent.page_no)
    )
    rows = result.all()

    data = []
    for page, doc in rows:
        cls = page.classification or {}
        row = {
            "页码": page.page_no,
            "文件名": doc.file_name,
            "文本内容": (page.content or "")[:3000],
            "材料分类": cls.get("材料分类", ""),
            "次分类": cls.get("次分类", ""),
            "研究映射": "；".join(cls.get("研究映射", [])),
            "是否正文": "是" if cls.get("是否正文") else "否",
            "质量等级": cls.get("质量等级", ""),
            "等级原因": cls.get("等级原因", ""),
            "文本密度分": cls.get("文本密度分", 0),
            "研究信号分": cls.get("研究信号分", 0),
            "推荐用途": cls.get("推荐用途", ""),
            "页面摘要": cls.get("页面摘要", ""),
            "证据句1": cls.get("证据句1", ""),
            "证据句2": cls.get("证据句2", ""),
        }
        data.append(row)

    # BE-012: 空数据检查
    if not data:
        raise ValueError("没有可导出的分类数据")

    export_dir = ensure_dir(get_export_dir(project_id))
    ext_map = {"excel": ".xlsx", "csv": ".csv", "json": ".json"}
    filename = f"页级分类结果{ext_map.get(format, '.xlsx')}"
    output_path = export_dir / filename

    if format == "csv":
        export_to_csv(data, output_path)
    elif format == "json":
        export_to_json(data, output_path)
    else:
        export_to_excel(data, output_path, sheet_name="页级分类结果")

    return output_path


async def export_base_table(
    db: AsyncSession,
    project_id: uuid.UUID,
    format: str = "excel",
) -> Path:
    """导出 OCR/页面导入后的底表：页码、文件名、每页内容。"""
    result = await db.execute(
        select(PageContent, SourceDocument)
        .join(SourceDocument, PageContent.document_id == SourceDocument.id)
        .where(SourceDocument.project_id == project_id)
        .order_by(SourceDocument.created_at, PageContent.page_no)
    )
    rows = result.all()

    data = []
    for page, doc in rows:
        content = page.content or page.raw_ocr_text or ""
        if not content:
            continue
        data.append(
            {
                "页码": page.page_no,
                "页面内容": content,
                "文件名": doc.file_name,
                "文件类型": doc.file_type,
                "OCR状态": page.ocr_status,
                "OCR供应商": page.ocr_provider or "",
                "OCR置信度": page.ocr_confidence if page.ocr_confidence is not None else "",
            }
        )

    if not data:
        raise ValueError("底表为空，请先完成 OCR 或 Excel 页面导入")

    export_dir = ensure_dir(get_export_dir(project_id))
    ext_map = {"excel": ".xlsx", "csv": ".csv", "json": ".json"}
    filename = f"底表_页面OCR内容{ext_map.get(format, '.xlsx')}"
    output_path = export_dir / filename

    if format == "csv":
        export_to_csv(data, output_path)
    elif format == "json":
        export_to_json(data, output_path)
    else:
        export_to_excel(data, output_path, sheet_name="底表")

    return output_path


async def export_document_base_table(
    db: AsyncSession,
    document_id: uuid.UUID,
    format: str = "excel",
) -> Path:
    """导出单个文件 OCR/页面导入后的底表。"""
    result = await db.execute(
        select(PageContent, SourceDocument)
        .join(SourceDocument, PageContent.document_id == SourceDocument.id)
        .where(PageContent.document_id == document_id)
        .order_by(PageContent.page_no)
    )
    rows = result.all()

    if not rows:
        raise ValueError("当前文件还没有可导出的页面内容，请先完成 OCR 或 Excel 页面导入")

    data = []
    doc_for_name = None
    for page, doc in rows:
        doc_for_name = doc
        content = page.content or page.raw_ocr_text or ""
        data.append(
            {
                "页码": page.page_no,
                "页面内容": content,
            }
        )

    if not any(row["页面内容"] for row in data):
        raise ValueError("当前文件的底表为空，请检查 OCR 是否成功识别出内容")

    export_dir = ensure_dir(get_export_dir(document_id))
    ext_map = {"excel": ".xlsx", "csv": ".csv", "json": ".json"}
    safe_name = sanitize_filename(Path(doc_for_name.file_name).stem if doc_for_name else "底表")
    output_path = export_dir / f"{safe_name}_底表_每页OCR内容{ext_map.get(format, '.xlsx')}"

    if format == "csv":
        export_to_csv(data, output_path)
    elif format == "json":
        export_to_json(data, output_path)
    else:
        export_to_excel(data, output_path, sheet_name="底表")

    return output_path


async def export_page_pool(
    db: AsyncSession,
    theme_id: uuid.UUID,
    format: str = "excel",
) -> Path:
    """导出页面池"""
    theme_result = await db.execute(select(ThemeConfig).where(ThemeConfig.id == theme_id))
    theme = theme_result.scalar_one_or_none()
    if theme is None:
        raise ValueError(f"专题不存在: {theme_id}")

    result = await db.execute(
        select(PagePool, PageContent)
        .join(PageContent, PagePool.page_id == PageContent.id)
        .where(PagePool.theme_id == theme_id, PagePool.is_latest == True)
        .order_by(PagePool.score.desc())
    )
    rows = result.all()

    data = []
    for pool, page in rows:
        cls = page.classification or {}
        row = {
            "页码": page.page_no,
            "总分": pool.score,
            "置信度": pool.置信度 or "",
            "等级": pool.relevance_level or "",
            "关键词命中数": pool.关键词命中数 or 0,
            "信号命中数": pool.信号命中数 or 0,
            "命中关键词": "、".join(pool.命中关键词 or []),
            "材料分类": cls.get("材料分类", ""),
            "质量等级": cls.get("质量等级", ""),
            "文本内容": (page.content or "")[:3000],
            "页面摘要": cls.get("页面摘要", ""),
        }
        data.append(row)

    # BE-012: 空数据检查
    if not data:
        raise ValueError("页面池为空，没有可导出的数据")

    export_dir = ensure_dir(get_export_dir(theme_id))
    ext_map = {"excel": ".xlsx", "csv": ".csv", "json": ".json"}
    safe_name = sanitize_filename(theme.theme)
    filename = f"{safe_name}专题页面池{ext_map.get(format, '.xlsx')}"
    output_path = export_dir / filename

    if format == "csv":
        export_to_csv(data, output_path)
    elif format == "json":
        export_to_json(data, output_path)
    else:
        export_to_excel(data, output_path, sheet_name=_sanitize_sheet_name(f"{theme.theme}专题页面池"))

    return output_path


async def export_narrative_units(
    db: AsyncSession,
    theme_id: uuid.UUID,
    format: str = "excel",
) -> Path:
    """导出叙事单元"""
    theme_result = await db.execute(select(ThemeConfig).where(ThemeConfig.id == theme_id))
    theme = theme_result.scalar_one_or_none()
    if theme is None:
        raise ValueError(f"专题不存在: {theme_id}")

    result = await db.execute(
        select(NarrativeUnit)
        .where(NarrativeUnit.theme_id == theme_id, NarrativeUnit.is_latest == True)
        .order_by(NarrativeUnit.source_page)
    )
    units = result.scalars().all()

    data = []
    all_field_keys = {key for unit in units for key in (unit.fields or {}).keys()}
    if theme.narrative_schema:
        schema_fields = [str(field) for field in theme.narrative_schema if str(field).strip()]
    elif "专题名称" in all_field_keys or "叙事单元标题" in all_field_keys:
        schema_fields: list[str] = list(GENERIC_NARRATIVE_EXPORT_FIELDS)
    else:
        schema_fields = []

    for unit in units:
        for key in (unit.fields or {}).keys():
            if key not in schema_fields and key not in {"来源页码", "置信度"}:
                schema_fields.append(key)

    preferred_first = ["专题名称", "叙事单元标题", "单元类型", "涉及对象"]
    schema_fields = (
        [field for field in preferred_first if field in schema_fields]
        + [field for field in schema_fields if field not in preferred_first]
    )

    for unit in units:
        fields = unit.fields or {}
        row = {"页码": unit.source_page}
        for field in schema_fields:
            if field in {"来源页码", "置信度"}:
                continue
            value = fields.get(field, "")
            if isinstance(value, list):
                value = "、".join(str(item) for item in value)
            elif isinstance(value, dict):
                value = str(value)
            if field == "原文证据" and isinstance(value, str):
                value = value[:700]
            row[field] = value
        row["置信度"] = unit.confidence
        data.append(row)

    # BE-012: 空数据检查
    if not data:
        raise ValueError("叙事单元为空，没有可导出的数据")

    export_dir = ensure_dir(get_export_dir(theme_id))
    ext_map = {"excel": ".xlsx", "csv": ".csv", "json": ".json"}
    safe_name = sanitize_filename(theme.theme)
    filename = f"{safe_name}叙事单元表{ext_map.get(format, '.xlsx')}"
    output_path = export_dir / filename

    if format == "csv":
        export_to_csv(data, output_path)
    elif format == "json":
        export_to_json(data, output_path)
    else:
        export_to_excel(data, output_path, sheet_name=_sanitize_sheet_name(f"{theme.theme}叙事单元"))

    return output_path
