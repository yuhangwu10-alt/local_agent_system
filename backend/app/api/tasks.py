import uuid

from fastapi import APIRouter, HTTPException
from sqlalchemy import select, update

from app.database import async_session
from app.models.project import SourceDocument
from app.models.task import Task
from app.services.task_manager import task_manager

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("/{task_id}")
async def get_task_status(task_id: uuid.UUID):
    """查询任务状态"""
    status = await task_manager.get_status(task_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return status


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: uuid.UUID):
    """取消任务"""
    async with async_session() as db:
        result = await db.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        document_id = (task.result or {}).get("document_id") if isinstance(task.result, dict) else None

    success = await task_manager.cancel(task_id)
    if not success:
        raise HTTPException(status_code=400, detail="Task cannot be cancelled")

    if document_id and task.task_type == "ocr":
        async with async_session() as db:
            await db.execute(
                update(SourceDocument)
                .where(SourceDocument.id == uuid.UUID(str(document_id)))
                .values(status="ocr_failed")
            )
            await db.commit()
    return {"status": "cancelled"}
