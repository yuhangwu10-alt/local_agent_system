import asyncio
import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, update, delete

from app.config import settings
from app.models.project import SourceDocument
from app.models.page import PageContent
from app.providers.ocr.base import OCRProvider
from app.providers.ocr.qwen_vl import QwenVLOCR
from app.providers.ocr.openai_vision import OpenAIVisionOCR
from app.services import pdf_service, excel_service
from app.services.task_manager import task_manager
from app.utils.file_storage import get_input_dir, get_page_images_dir, safe_join, ensure_dir

logger = logging.getLogger(__name__)

# 空白页判定阈值：OCR 结果少于此字符数视为无实际内容
BLANK_PAGE_CHAR_THRESHOLD = 5

# Provider 单例缓存
_ocr_provider_instance: OCRProvider | None = None


def get_ocr_provider(runtime_config: dict | None = None) -> OCRProvider:
    if runtime_config:
        provider_id = runtime_config.get("provider") or settings.ocr_provider
        if provider_id in {"dashscope", "mimo", "deepseek", "glm", "kimi", "siliconflow"}:
            return QwenVLOCR.from_runtime_config(runtime_config)

    global _ocr_provider_instance
    if _ocr_provider_instance is None:
        providers = {
            "qwen_vl": QwenVLOCR,
            "openai_vision": OpenAIVisionOCR,
        }
        cls = providers.get(settings.ocr_provider)
        if cls is None:
            raise ValueError(f"Unknown OCR provider: {settings.ocr_provider}")
        _ocr_provider_instance = cls()
    return _ocr_provider_instance


@dataclass
class DocInfo:
    """文档信息的普通数据类，避免 session 外使用 ORM 对象"""
    id: UUID
    project_id: UUID
    file_type: str
    file_path: str


def _normalize_ocr_batch_size(value) -> int:
    try:
        batch_size = int(value)
    except (TypeError, ValueError):
        batch_size = 1
    return max(1, min(50, batch_size))


def _normalize_ocr_runtime_config(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    provider = str(value.get("provider") or "").strip()
    api_key = str(value.get("api_key") or "").strip()
    model = str(value.get("model") or "").strip()
    prompt = str(value.get("prompt") or "").strip()
    if not provider or not api_key or not model:
        return None
    config = {"provider": provider, "api_key": api_key, "model": model}
    if prompt:
        config["prompt"] = prompt
    return config


async def run_ocr_task(task_id: UUID, document_id: str, **kwargs):
    """OCR 异步任务入口（BE-001: 外层 try/except 保证状态同步）"""
    from app.database import async_session

    doc_id = UUID(document_id)
    t_id = task_id

    async with async_session() as db:
        result = await db.execute(select(SourceDocument).where(SourceDocument.id == doc_id))
        doc = result.scalar_one_or_none()
        if doc is None:
            raise ValueError(f"文档不存在: {doc_id}")
        doc_info = DocInfo(
            id=doc.id,
            project_id=doc.project_id,
            file_type=doc.file_type,
            file_path=doc.file_path,
        )


    try:
        batch_size = _normalize_ocr_batch_size(kwargs.get("ocr_batch_size"))
        runtime_config = _normalize_ocr_runtime_config(kwargs.get("ocr_config"))

        recovered = bool(kwargs.get("recovered"))

        if doc_info.file_type == "pdf":
            failed_ratio = await _ocr_pdf(
                doc_info,
                t_id,
                batch_size=batch_size,
                runtime_config=runtime_config,
                recovered=recovered,
            )
        else:
            failed_ratio = await _ocr_excel(doc_info, t_id, recovered=recovered)

        # BE-003: 失败页比例过高则判定任务失败
        if failed_ratio > settings.ocr_fail_threshold:
            async with async_session() as db:
                await db.execute(
                    update(SourceDocument)
                    .where(SourceDocument.id == doc_id)
                    .values(status="ocr_failed")
                )
                await db.commit()
            raise RuntimeError(f"OCR 失败率过高: {failed_ratio:.0%}，超过阈值 {settings.ocr_fail_threshold:.0%}")

        # 成功
        async with async_session() as db:
            await db.execute(
                update(SourceDocument)
                .where(SourceDocument.id == doc_id)
                .values(status="ocr_completed")
            )
            await db.commit()
            from app.services.export_service import export_document_base_table

            base_table_path = await export_document_base_table(db, doc_info.id, "excel")

    except asyncio.CancelledError:
        # BE-FIX-003: 取消时也要更新文档状态
        logger.info(f"OCR task cancelled for document {doc_id}")
        async with async_session() as db:
            await db.execute(
                update(SourceDocument)
                .where(SourceDocument.id == doc_id)
                .values(status="ocr_failed")
            )
            await db.commit()
        raise

    except Exception as e:
        # BE-001: 异常时更新文档状态为 ocr_failed
        logger.exception(f"OCR task failed for document {doc_id}: {e}")
        async with async_session() as db:
            await db.execute(
                update(SourceDocument)
                .where(SourceDocument.id == doc_id)
                .values(status="ocr_failed")
            )
            await db.commit()
        raise

    return {
        "文档ID": str(doc_id),
        "状态": "完成",
        "失败率": f"{failed_ratio:.1%}",
        "底表文件": str(base_table_path),
    }


async def _ocr_pdf(
    doc: DocInfo,
    task_id: UUID,
    *,
    batch_size: int = 1,
    runtime_config: dict | None = None,
    recovered: bool = False,
) -> float:
    """处理 PDF 文档的 OCR（逐页处理，避免内存问题），返回失败页比例"""
    from app.database import async_session

    input_dir = get_input_dir(doc.project_id)
    pdf_path = safe_join(input_dir, doc.file_path)
    total_pages = await pdf_service.get_pdf_page_count(pdf_path)

    # 新任务默认清理旧页面；服务重启恢复时保留已落库页面并跳过。
    async with async_session() as db:
        if not recovered:
            await db.execute(delete(PageContent).where(PageContent.document_id == doc.id))
        await db.execute(
            update(SourceDocument)
            .where(SourceDocument.id == doc.id)
            .values(status="ocr_processing", total_pages=total_pages)
        )
        await db.commit()

    provider = get_ocr_provider(runtime_config)
    images_dir = ensure_dir(get_page_images_dir(doc.id))
    db_batch: list[PageContent] = []
    db_batch_size = 50

    async with async_session() as db:
        existing_result = await db.execute(
            select(PageContent).where(PageContent.document_id == doc.id)
        )
        existing_pages = existing_result.scalars().all()
        existing_page_nos = {page.page_no for page in existing_pages}

    remaining_page_numbers = [page_no for page_no in range(1, total_pages + 1) if page_no not in existing_page_nos]

    async def process_page(page_no: int) -> PageContent:
        try:
            img_bytes = await pdf_service.get_page_image(pdf_path, page_no)
            img_path = images_dir / f"page_{page_no:04d}.jpg"
            img_path.write_bytes(img_bytes)

            text, confidence = await provider.recognize(img_bytes)
            page = PageContent(
                document_id=doc.id,
                page_no=page_no,
                raw_ocr_text=text,
                content=text,
                ocr_status="completed",
                ocr_provider=(runtime_config or {}).get("provider") or settings.ocr_provider,
                ocr_confidence=confidence,
            )
        except Exception as e:
            logger.error(f"OCR 失败 第{page_no}页: {e}")
            return PageContent(
                document_id=doc.id,
                page_no=page_no,
                raw_ocr_text="",
                content="",
                ocr_status="failed",
                ocr_provider=(runtime_config or {}).get("provider") or settings.ocr_provider,
                ocr_confidence=0.0,
                ocr_error=str(e),
            )
        return page

    # 按用户配置的批次窗口处理。批次页数受 API 厂商并发/限流能力影响。
    completed_pages = len(existing_page_nos)
    await task_manager.update_progress(
        task_id,
        int(completed_pages / total_pages * 90) + 5 if total_pages else 5,
        {"current": completed_pages, "total": total_pages, "type": "ocr", "document_id": str(doc.id)},
    )
    for start in range(0, len(remaining_page_numbers), batch_size):
        page_numbers = remaining_page_numbers[start:start + batch_size]
        pages = await asyncio.gather(*(process_page(page_no) for page_no in page_numbers))

        db_batch.extend(pages)

        if len(db_batch) >= db_batch_size:
            async with async_session() as db:
                db.add_all(db_batch)
                await db.commit()
            db_batch = []

        completed_pages += len(page_numbers)
        progress = int(completed_pages / total_pages * 90) + 5
        await task_manager.update_progress(
            task_id,
            progress,
            {"current": completed_pages, "total": total_pages, "type": "ocr", "document_id": str(doc.id)},
        )

    if db_batch:
        async with async_session() as db:
            db.add_all(db_batch)
            await db.commit()

    # 后处理：检测空白页并标记
    blank_count = 0
    async with async_session() as db:
        result = await db.execute(
            select(PageContent).where(PageContent.document_id == doc.id)
        )
        all_pages = result.scalars().all()
        failed_count = 0
        for page in all_pages:
            if page.ocr_status == "failed":
                failed_count += 1
                continue
            text = (page.content or "").strip()
            # 空白页判定：内容过短或为 OCR 模型返回的"无文字"标识
            if len(text) < BLANK_PAGE_CHAR_THRESHOLD or text in ("无文字", "无", "无字", "空白页"):
                page.ocr_status = "blank"
                page.content = ""
                blank_count += 1
        await db.commit()

    # 计算实际有内容的页数
    content_pages = total_pages - blank_count - failed_count
    logger.info(f"PDF 处理完成：物理页数={total_pages}，空白页={blank_count}，失败页={failed_count}，内容页={content_pages}")

    # 更新文档的内容页数
    async with async_session() as db:
        await db.execute(
            update(SourceDocument)
            .where(SourceDocument.id == doc.id)
            .values(total_pages=content_pages if content_pages > 0 else total_pages)
        )
        await db.commit()

    return failed_count / total_pages if total_pages > 0 else 0.0


async def _ocr_excel(doc: DocInfo, task_id: UUID, *, recovered: bool = False) -> float:
    """处理 Excel 文档，返回失败比例"""
    from app.database import async_session

    input_dir = get_input_dir(doc.project_id)
    excel_path = safe_join(input_dir, doc.file_path)
    pages = await excel_service.read_ocr_excel(excel_path)

    # 新任务默认清理旧页面；服务重启恢复时保留已落库页面并跳过。
    async with async_session() as db:
        if not recovered:
            await db.execute(delete(PageContent).where(PageContent.document_id == doc.id))
        await db.execute(
            update(SourceDocument)
            .where(SourceDocument.id == doc.id)
            .values(status="ocr_processing", total_pages=len(pages))
        )
        await db.commit()

    batch: list[PageContent] = []
    batch_size = 50
    async with async_session() as db:
        existing_result = await db.execute(
            select(PageContent.page_no).where(PageContent.document_id == doc.id)
        )
        existing_page_nos = set(existing_result.scalars().all())

    for idx, page_data in enumerate(pages):
        if page_data["page_no"] in existing_page_nos:
            progress = int((idx + 1) / len(pages) * 90) + 5
            await task_manager.update_progress(
                task_id,
                progress,
                {"current": idx + 1, "total": len(pages), "type": "ocr", "document_id": str(doc.id)},
            )
            continue
        page = PageContent(
            document_id=doc.id,
            page_no=page_data["page_no"],
            raw_ocr_text=None,
            content=page_data["content"],
            ocr_status="completed",
            ocr_provider="excel_import",
            ocr_confidence=1.0,
        )
        batch.append(page)

        if len(batch) >= batch_size:
            async with async_session() as db:
                db.add_all(batch)
                await db.commit()
            batch = []

        progress = int((idx + 1) / len(pages) * 90) + 5
        await task_manager.update_progress(
            task_id,
            progress,
            {"current": idx + 1, "total": len(pages), "type": "ocr", "document_id": str(doc.id)},
        )

    if batch:
        async with async_session() as db:
            db.add_all(batch)
            await db.commit()

    return 0.0  # Excel 导入不会有 OCR 失败
