import asyncio
import logging
import uuid
from collections.abc import Callable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.project import SourceDocument
from app.models.task import Task

logger = logging.getLogger(__name__)


def public_task_payload(payload):
    if isinstance(payload, dict):
        redacted = {}
        for key, value in payload.items():
            if key.lower() in {"api_key", "apikey", "token", "secret"}:
                redacted[key] = "***"
            else:
                redacted[key] = public_task_payload(value)
        return redacted
    if isinstance(payload, list):
        return [public_task_payload(item) for item in payload]
    return payload


class TaskManager:
    """基于 asyncio 的轻量任务管理器，替代 Celery"""

    def __init__(self):
        self._tasks: dict[uuid.UUID, asyncio.Task] = {}

    def _task_registry(self) -> dict[str, Callable]:
        from app.services.classification_service import run_classification
        from app.services.keyword_completion_service import run_keyword_completion
        from app.services.narrative_service import run_narrative_extraction
        from app.services.ocr_service import run_ocr_task
        from app.services.page_pool_service import (
            run_multi_theme_page_pool_generation,
            run_page_pool_generation,
        )
        from app.services.topic_extraction_service import run_topic_extraction

        return {
            "classification": run_classification,
            "keyword_completion": run_keyword_completion,
            "narrative": run_narrative_extraction,
            "ocr": run_ocr_task,
            "page_pool": run_page_pool_generation,
            "page_pool_batch": run_multi_theme_page_pool_generation,
            "topic_extraction": run_topic_extraction,
        }

    def _start_task(
        self,
        task_id: uuid.UUID,
        task_type: str,
        coro_func: Callable,
        *,
        recovered: bool = False,
        **kwargs,
    ) -> None:
        if task_id in self._tasks and not self._tasks[task_id].done():
            return

        async def _wrapped():
            try:
                async with async_session() as db:
                    await db.execute(
                        update(Task).where(Task.id == task_id).values(status="running")
                    )
                    await db.commit()

                result = await coro_func(task_id=task_id, recovered=recovered, **kwargs)

                async with async_session() as db:
                    await db.execute(
                        update(Task)
                        .where(Task.id == task_id)
                        .values(status="completed", progress=100, result=result)
                    )
                    await db.commit()

            except asyncio.CancelledError:
                logger.info(f"Task {task_id} cancelled")
                try:
                    async with async_session() as db:
                        await db.execute(
                            update(Task)
                            .where(Task.id == task_id)
                            .values(status="cancelled", error="用户取消")
                        )
                        await db.commit()
                except Exception as db_err:
                    logger.error(f"Failed to update cancelled task {task_id} status: {db_err}")

            except Exception as e:
                logger.exception(f"Task {task_id} failed: {e}")
                try:
                    async with async_session() as db:
                        await db.execute(
                            update(Task)
                            .where(Task.id == task_id)
                            .values(status="failed", error=str(e))
                        )
                        await db.commit()
                except Exception as db_err:
                    logger.error(f"Failed to update failed task {task_id} status: {db_err}")

            finally:
                self._tasks.pop(task_id, None)

        logger.info("Starting task %s (%s), recovered=%s", task_id, task_type, recovered)
        self._tasks[task_id] = asyncio.create_task(_wrapped())

    async def submit(
        self,
        task_type: str,
        project_id: uuid.UUID,
        coro_func: Callable,
        **kwargs,
    ) -> uuid.UUID:
        """提交异步任务，返回 task_id"""
        async with async_session() as db:
            task = Task(
                project_id=project_id,
                task_type=task_type,
                status="pending",
                progress=0,
                payload=kwargs,
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
            task_id = task.id

        self._start_task(task_id, task_type, coro_func, **kwargs)
        return task_id

    async def update_progress(self, task_id: uuid.UUID, progress: int, meta: dict | None = None):
        """更新任务进度。meta 可包含 {current, total} 用于前端展示 m/N"""
        try:
            async with async_session() as db:
                values = {"progress": progress}
                if meta:
                    result = await db.execute(select(Task.result).where(Task.id == task_id))
                    current_result = result.scalar_one_or_none()
                    if isinstance(current_result, dict):
                        values["result"] = {**current_result, **meta}
                    else:
                        values["result"] = meta
                await db.execute(
                    update(Task).where(Task.id == task_id).values(**values)
                )
                await db.commit()
        except Exception as e:
            logger.error(f"Failed to update progress for task {task_id}: {e}")

    async def get_status(self, task_id: uuid.UUID) -> dict | None:
        """查询任务状态"""
        async with async_session() as db:
            result = await db.execute(select(Task).where(Task.id == task_id))
            task = result.scalar_one_or_none()
            if task is None:
                return None
            return {
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

    async def cancel(self, task_id: uuid.UUID) -> bool:
        """取消任务（只发送取消信号，状态更新由 _wrapped 处理）"""
        atask = self._tasks.get(task_id)
        if atask and not atask.done():
            atask.cancel()
            return True
        return False

    async def recover_on_startup(self):
        """进程重启时恢复仍在运行的任务。"""
        registry = self._task_registry()
        recoverable: list[Task] = []
        async with async_session() as db:
            result = await db.execute(
                select(Task).where(Task.status.in_(["running", "pending"]))
            )
            stale_tasks = result.scalars().all()
            for task in stale_tasks:
                if task.payload and task.task_type in registry:
                    recoverable.append(task)
                else:
                    await db.execute(
                        update(Task)
                        .where(Task.id == task.id)
                        .values(status="failed", error="Server restarted before task payload recovery was available")
                    )

            await db.execute(
                update(SourceDocument)
                .where(SourceDocument.status == "ocr_processing")
                .values(status="registered")
            )
            await db.commit()

        for task in recoverable:
            payload = dict(task.payload or {})
            if task.task_type == "ocr":
                document_id = payload.get("document_id")
                if document_id:
                    async with async_session() as db:
                        await db.execute(
                            update(SourceDocument)
                            .where(SourceDocument.id == uuid.UUID(str(document_id)))
                            .values(status="ocr_processing")
                        )
                        await db.commit()
            self._start_task(
                task.id,
                task.task_type,
                registry[task.task_type],
                recovered=True,
                **payload,
            )
        logger.info("Recovered %s resumable tasks on startup", len(recoverable))


task_manager = TaskManager()
