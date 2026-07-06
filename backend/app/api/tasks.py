import uuid

from fastapi import APIRouter, HTTPException
from sqlalchemy import select, update

from app.database import async_session
from app.models.project import SourceDocument
from app.models.task import Task
from app.services.task_manager import public_task_payload, task_manager

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("/projects/{project_id}/active")
async def list_active_project_tasks(project_id: uuid.UUID):
    """列出项目下仍在等待或运行的任务，用于页面刷新后恢复轮询。"""
    async with async_session() as db:
        result = await db.execute(
            select(Task)
            .where(
                Task.project_id == project_id,
                Task.status.in_(["pending", "running"]),
            )
            .order_by(Task.created_at.asc())
        )
        tasks = result.scalars().all()
    return [
        {
            "id": task.id,
            "project_id": task.project_id,
            "task_type": task.task_type,
            "status": task.status,
            "progress": task.progress,
            "payload": public_task_payload(task.payload),
            "result": task.result,
            "error": task.error,
            "created_at": task.created_at.isoformat(),
            "updated_at": task.updated_at.isoformat(),
        }
        for task in tasks
    ]


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
