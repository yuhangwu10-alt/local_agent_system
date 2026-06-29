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


class TaskManager:
    """基于 asyncio 的轻量任务管理器，替代 Celery"""

    def __init__(self):
        self._tasks: dict[uuid.UUID, asyncio.Task] = {}

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
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
            task_id = task.id

        async def _wrapped():
            try:
                async with async_session() as db:
                    await db.execute(
                        update(Task).where(Task.id == task_id).values(status="running")
                    )
                    await db.commit()

                result = await coro_func(task_id=task_id, **kwargs)

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

        atask = asyncio.create_task(_wrapped())
        self._tasks[task_id] = atask
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
        """进程重启时恢复：将 running/pending 任务标记为 failed，将 processing 文档重置"""
        async with async_session() as db:
            await db.execute(
                update(Task)
                .where(Task.status.in_(["running", "pending"]))
                .values(status="failed", error="Server restarted")
            )
            await db.execute(
                update(SourceDocument)
                .where(SourceDocument.status == "ocr_processing")
                .values(status="registered")
            )
            await db.commit()
        logger.info("Recovered stale tasks and documents on startup")


task_manager = TaskManager()
