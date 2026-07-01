import logging
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Body
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

from app.api.deps import get_db
from app.models.task import Task
from app.models.project import Project, SourceDocument
from app.schemas.project import DocumentResponse, ScanResponse
from app.services.file_scanner import scan_input_directory
from app.services.task_manager import task_manager
from app.services.ocr_service import run_ocr_task
from app.services.classification_service import run_classification
from app.services.topic_extraction_service import run_topic_extraction
from app.utils.file_storage import get_input_dir, get_page_images_dir, ensure_dir, safe_join, sanitize_filename

router = APIRouter(prefix="/api/documents", tags=["documents"])

ALLOWED_EXTENSIONS = {".pdf", ".xlsx"}
MAX_FILE_SIZE = 500 * 1024 * 1024  # 500MB


@router.post("/scan", response_model=ScanResponse)
async def scan_documents(project_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """扫描 input/ 目录，发现并注册新文件"""
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    input_dir = ensure_dir(get_input_dir(project_id))
    new_files = await scan_input_directory(input_dir, project_id, db)
    return ScanResponse(new_files=new_files, count=len(new_files))


@router.post("/upload", response_model=DocumentResponse, status_code=201)
async def upload_document(
    project_id: uuid.UUID = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """HTTP 上传文件"""
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    # 校验文件名
    original_name = file.filename or "unnamed"
    suffix = Path(original_name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"不支持的文件类型: {suffix}，允许: {ALLOWED_EXTENSIONS}")

    # 清理文件名，原子写入防 TOCTOU 竞态（"xb" 模式 = 文件存在则失败）
    safe_name = sanitize_filename(original_name)
    input_dir = ensure_dir(get_input_dir(project_id))
    file_path = input_dir / safe_name
    try:
        f = open(file_path, "xb")
    except FileExistsError:
        stem = Path(safe_name).stem
        ext = Path(safe_name).suffix
        safe_name = f"{stem}_{uuid.uuid4().hex[:8]}{ext}"
        file_path = input_dir / safe_name
        f = open(file_path, "xb")

    # BE-006: 分块读取，避免大文件一次性加载到内存
    CHUNK_SIZE = 1024 * 1024  # 1MB
    total_size = 0
    success = False
    try:
        while True:
            chunk = await file.read(CHUNK_SIZE)
            if not chunk:
                break
            total_size += len(chunk)
            if total_size > MAX_FILE_SIZE:
                raise HTTPException(status_code=413, detail=f"文件过大，最大允许 {MAX_FILE_SIZE // 1024 // 1024}MB")
            f.write(chunk)
        success = True
    finally:
        f.close()
        if not success:
            file_path.unlink(missing_ok=True)

    file_type = "pdf" if suffix == ".pdf" else "excel"

    doc = SourceDocument(
        project_id=project_id,
        file_type=file_type,
        file_path=safe_name,
        file_name=original_name,
        status="registered",
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return doc


@router.get("/projects/{project_id}", response_model=list[DocumentResponse])
async def list_documents(project_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(SourceDocument)
        .where(SourceDocument.project_id == project_id)
        .order_by(SourceDocument.created_at.desc())
    )
    return result.scalars().all()


@router.post("/{document_id}/ocr")
async def trigger_ocr(
    document_id: uuid.UUID,
    payload: dict = Body(default_factory=dict),
    db: AsyncSession = Depends(get_db),
):
    """触发 OCR 任务"""
    result = await db.execute(select(SourceDocument).where(SourceDocument.id == document_id))
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")

    if doc.status == "ocr_processing":
        raise HTTPException(status_code=400, detail="OCR 正在进行中，请等待完成")
    # ocr_completed 和 ocr_failed 都允许重跑（BE-002 幂等）
    await db.execute(
        update(SourceDocument)
        .where(SourceDocument.id == document_id)
        .values(status="ocr_processing")
    )
    await db.commit()

    task_id = await task_manager.submit(
        task_type="ocr",
        project_id=doc.project_id,
        coro_func=run_ocr_task,
        document_id=str(document_id),
        ocr_batch_size=payload.get("ocr_batch_size"),
        ocr_config=payload.get("ocr_config"),
    )
    await db.execute(
        update(Task)
        .where(Task.id == task_id)
        .values(result={"type": "ocr", "document_id": str(document_id), "current": 0, "total": doc.total_pages or 0})
    )
    await db.commit()

    return {"task_id": str(task_id), "status": "submitted"}


@router.post("/{document_id}/ocr/cancel")
async def cancel_document_ocr(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """按文档取消正在运行的 OCR/页面导入任务。"""
    result = await db.execute(select(SourceDocument).where(SourceDocument.id == document_id))
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")

    task_rows = await db.execute(
        select(Task)
        .where(
            Task.project_id == doc.project_id,
            Task.task_type == "ocr",
            Task.status.in_(["pending", "running"]),
        )
        .order_by(Task.created_at.desc())
    )
    matched_task = None
    for task in task_rows.scalars().all():
        result_meta = task.result if isinstance(task.result, dict) else {}
        if result_meta.get("document_id") == str(document_id):
            matched_task = task
            break

    if matched_task is None:
        if doc.status == "ocr_processing":
            await db.execute(
                update(SourceDocument)
                .where(SourceDocument.id == document_id)
                .values(status="ocr_failed")
            )
            await db.commit()
            return {"status": "cancelled", "message": "未找到运行中的任务，已解锁文档状态"}
        raise HTTPException(status_code=400, detail="当前文档没有可取消的 OCR 任务")

    success = await task_manager.cancel(matched_task.id)
    if not success:
        raise HTTPException(status_code=400, detail="任务已结束或无法取消")

    await db.execute(
        update(SourceDocument)
        .where(SourceDocument.id == document_id)
        .values(status="ocr_failed")
    )
    await db.commit()
    return {"status": "cancelled", "task_id": str(matched_task.id)}


@router.post("/{document_id}/classify")
async def trigger_document_classification(
    document_id: uuid.UUID,
    payload: dict = Body(default_factory=dict),
    db: AsyncSession = Depends(get_db),
):
    """触发单个文件的页级分类任务。"""
    result = await db.execute(select(SourceDocument).where(SourceDocument.id == document_id))
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.status != "ocr_completed":
        raise HTTPException(status_code=400, detail="当前文件还没有完成 OCR/页面导入")

    task_id = await task_manager.submit(
        task_type="classification",
        project_id=doc.project_id,
        coro_func=run_classification,
        proj_id=str(doc.project_id),
        document_id=str(document_id),
        llm_config=payload.get("llm_config"),
        llm_concurrency=payload.get("llm_concurrency", 5),
    )

    return {"task_id": str(task_id), "status": "submitted"}


@router.post("/projects/{project_id}/classify")
async def trigger_classification(project_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """触发页级分类任务"""
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")

    task_id = await task_manager.submit(
        task_type="classification",
        project_id=project_id,
        coro_func=run_classification,
        proj_id=str(project_id),
    )

    return {"task_id": str(task_id), "status": "submitted"}


@router.post("/{document_id}/extract-topics")
async def extract_document_topics(
    document_id: uuid.UUID,
    payload: dict = Body(default_factory=dict),
    db: AsyncSession = Depends(get_db),
):
    """触发批量专题提取任务：分批调 LLM 发现专题，最终合并去重。"""
    result = await db.execute(select(SourceDocument).where(SourceDocument.id == document_id))
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    if doc.status != "ocr_completed":
        raise HTTPException(status_code=400, detail="请先完成 OCR 处理再进行专题提取")

    task_id = await task_manager.submit(
        task_type="topic_extraction",
        project_id=doc.project_id,
        coro_func=run_topic_extraction,
        proj_id=str(doc.project_id),
        document_id=str(document_id),
        llm_config=payload.get("llm_config"),
        batch_size=payload.get("batch_size", 100),
        llm_concurrency=payload.get("llm_concurrency", 1),
    )

    return {"task_id": str(task_id), "status": "submitted", "message": "批次专题提取任务已提交"}


@router.delete("/{document_id}", status_code=204)
async def delete_document(document_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(SourceDocument).where(SourceDocument.id == document_id))
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")

    doc_path = get_input_dir(doc.project_id) / doc.file_path
    doc_name = doc.file_name

    # BE-FIX-021: 先清理 PagePool 中指向本文档页面的记录（page_id FK 无 CASCADE，且 NOT NULL）
    from app.models.page import PagePool as _PP, PageContent as _PC
    pool_ids = await db.execute(
        select(_PP.id)
        .join(_PC, _PP.page_id == _PC.id)
        .where(_PC.document_id == document_id)
    )
    for (pid,) in pool_ids.fetchall():
        pool_entry = await db.get(_PP, pid)
        if pool_entry is not None:
            await db.delete(pool_entry)

    # BE-014: 删除数据库记录（CASCADE 会删除关联的 PageContent）
    await db.delete(doc)
    await db.commit()

    # BE-014: 再清理物理文件
    try:
        if doc_path.exists():
            doc_path.unlink()
        # 清理页面图片目录
        page_images_dir = get_page_images_dir(document_id)
        if page_images_dir.exists():
            shutil.rmtree(page_images_dir)
    except Exception as e:
        logger.warning(f"清理文档文件失败 {doc_name}: {e}")


@router.get("/{document_id}/topics")
async def get_document_topics(document_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """获取已保存的专题列表"""
    result = await db.execute(select(SourceDocument).where(SourceDocument.id == document_id))
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    topics = doc.saved_topics or {}
    return {"topics": topics.get("专题列表", []), "updated_at": topics.get("updated_at", "")}


@router.put("/{document_id}/topics")
async def save_document_topics(
    document_id: uuid.UUID,
    payload: dict = Body(default_factory=dict),
    db: AsyncSession = Depends(get_db),
):
    """保存专题列表，支持前端在任意时刻持久化当前专题状态"""
    result = await db.execute(select(SourceDocument).where(SourceDocument.id == document_id))
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")

    topics = payload.get("专题列表") or payload.get("topics") or []
    from datetime import datetime as _dt
    doc.saved_topics = {
        "专题列表": topics,
        "updated_at": _dt.now().isoformat(),
    }
    await db.commit()
    return {"status": "saved", "count": len(topics)}
