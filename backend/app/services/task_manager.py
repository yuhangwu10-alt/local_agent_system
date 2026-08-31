import asyncio
import logging
import os
import socket
import uuid
from datetime import datetime, timedelta, timezone
from collections.abc import Callable

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.config import settings
from app.models.project import Project
from app.services.billing_service import reserve_quote, settle_quote, refund_quote
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
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"

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

        async def _heartbeat(stop_event: asyncio.Event):
            while not stop_event.is_set():
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=max(5, settings.task_heartbeat_seconds))
                except asyncio.TimeoutError:
                    async with async_session() as db:
                        now = datetime.now(timezone.utc)
                        await db.execute(update(Task).where(
                            Task.id == task_id,
                            Task.status == "running",
                            Task.worker_id == self.worker_id,
                        ).values(
                            heartbeat_at=now,
                            lease_expires_at=now + timedelta(seconds=max(30, settings.task_lease_seconds)),
                        ))
                        await db.commit()

        async def _wrapped():
            stop_heartbeat = asyncio.Event()
            heartbeat_task = None
            try:
                async with async_session() as db:
                    now = datetime.now(timezone.utc)
                    claim = await db.execute(update(Task).where(
                        Task.id == task_id,
                        Task.status.in_(["pending", "running"]),
                        or_(
                            Task.worker_id == self.worker_id,
                            Task.worker_id.is_(None),
                            Task.lease_expires_at.is_(None),
                            Task.lease_expires_at < now,
                        ),
                    ).values(
                        status="running", attempt_count=Task.attempt_count + 1,
                        heartbeat_at=now,
                        worker_id=self.worker_id,
                        lease_expires_at=now + timedelta(seconds=max(30, settings.task_lease_seconds)),
                    ))
                    await db.commit()
                    if claim.rowcount != 1:
                        logger.info("Task %s was claimed by another worker", task_id)
                        return

                heartbeat_task = asyncio.create_task(_heartbeat(stop_heartbeat))

                result = await asyncio.wait_for(
                    coro_func(task_id=task_id, recovered=recovered, **kwargs),
                    timeout=max(60, settings.task_timeout_seconds),
                )

                async with async_session() as db:
                    task_row = await db.get(Task, task_id)
                    if task_row is None:
                        return
                    if task_row.worker_id != self.worker_id:
                        logger.warning("Task %s lease changed before completion", task_id)
                        return
                    if task_row.status in {"cancelled", "failed"}:
                        return
                    if task_row.billing_quote_id and task_row.charge_status == "frozen":
                        await settle_quote(db, task_row.billing_quote_id, task_row.user_id)
                        task_row.charge_status = "settled"
                    await db.execute(update(Task).where(
                        Task.id == task_id,
                        Task.worker_id == self.worker_id,
                        Task.status.not_in(["completed", "failed", "cancelled"])
                    ).values(status="completed", progress=100, result=result,
                            heartbeat_at=datetime.now(timezone.utc), worker_id=None, lease_expires_at=None))
                    await db.commit()

            except asyncio.CancelledError:
                logger.info("Task %s cancelled", task_id)
                try:
                    async with async_session() as db:
                        task_row = await db.get(Task, task_id)
                        if task_row and task_row.worker_id == self.worker_id and task_row.billing_quote_id and task_row.charge_status == "frozen":
                            await refund_quote(db, task_row.billing_quote_id, task_row.user_id, "任务取消退款")
                            task_row.charge_status = "refunded"
                        await db.execute(update(Task).where(Task.id == task_id, Task.worker_id == self.worker_id).values(status="cancelled", error="用户取消", worker_id=None, lease_expires_at=None))
                        await db.commit()
                except Exception as db_err:
                    logger.error("Failed to update cancelled task %s: %s", task_id, db_err)

            except asyncio.TimeoutError:
                logger.error("Task %s timed out after %ss", task_id, settings.task_timeout_seconds)
                try:
                    async with async_session() as db:
                        task_row = await db.get(Task, task_id)
                        if task_row and task_row.worker_id == self.worker_id and task_row.billing_quote_id and task_row.charge_status == "frozen":
                            await refund_quote(db, task_row.billing_quote_id, task_row.user_id, "任务超时退款")
                            task_row.charge_status = "refunded"
                        await db.execute(update(Task).where(Task.id == task_id, Task.worker_id == self.worker_id).values(status="failed", error="任务执行超时", worker_id=None, lease_expires_at=None))
                        await db.commit()
                except Exception as db_err:
                    logger.error("Failed to update timed out task %s: %s", task_id, db_err)
            except Exception as e:
                logger.exception("Task %s failed: %s", task_id, e)
                try:
                    async with async_session() as db:
                        task_row = await db.get(Task, task_id)
                        if task_row and task_row.worker_id == self.worker_id and task_row.billing_quote_id and task_row.charge_status == "frozen":
                            await refund_quote(db, task_row.billing_quote_id, task_row.user_id)
                            task_row.charge_status = "refunded"
                        await db.execute(update(Task).where(Task.id == task_id, Task.worker_id == self.worker_id).values(status="failed", error=str(e), worker_id=None, lease_expires_at=None))
                        await db.commit()
                except Exception as db_err:
                    logger.error("Failed to update failed task %s: %s", task_id, db_err)

            finally:
                stop_heartbeat.set()
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
                    await asyncio.gather(heartbeat_task, return_exceptions=True)
                self._tasks.pop(task_id, None)

        logger.info("Starting task %s (%s), recovered=%s", task_id, task_type, recovered)
        self._tasks[task_id] = asyncio.create_task(_wrapped())

    async def submit(
        self,
        task_type: str,
        project_id: uuid.UUID,
        coro_func: Callable,
        *,
        user_id: uuid.UUID | None = None,
        quote_id: uuid.UUID | None = None,
        **kwargs,
    ) -> uuid.UUID:
        """Persist and start one task, reserving an optional billing quote."""
        if quote_id is not None:
            try:
                quote_id = uuid.UUID(str(quote_id))
            except (TypeError, ValueError) as exc:
                raise ValueError("报价 ID 无效") from exc
        async with async_session() as db:
            project = await db.get(Project, project_id)
            if project is None or project.user_id is None:
                raise ValueError("项目不存在或没有所属用户")
            resolved_user_id = user_id or project.user_id
            active_count = await db.scalar(select(func.count(Task.id)).where(
                Task.user_id == resolved_user_id,
                Task.status.in_(["pending", "running"])
            ))
            if (active_count or 0) >= settings.max_active_tasks_per_user:
                raise ValueError("当前活动任务已达到上限，请等待已有任务完成")
            if task_type == "ocr" and settings.require_quote_for_ocr and quote_id is None:
                raise ValueError("OCR 任务需要先获取并确认报价")
            charge_status = "none"
            safe_kwargs = dict(kwargs)
            for key in ("llm_config", "ocr_config", "llm_concurrency", "ocr_concurrency", "api_key", "token", "secret"):
                safe_kwargs.pop(key, None)
            if quote_id:
                await reserve_quote(db, quote_id, resolved_user_id)
                from app.models.platform import BillingQuote
                quote = await db.get(BillingQuote, quote_id)
                if quote is None or quote.project_id != project_id:
                    raise ValueError("报价与项目不匹配")
                if quote.task_id is not None:
                    raise ValueError("报价已绑定其他任务")
                quoted_documents = {str(item) for item in (quote.document_ids or [])}
                requested_document_ids = safe_kwargs.get("document_ids")
                if requested_document_ids is None and safe_kwargs.get("document_id"):
                    requested_document_ids = [safe_kwargs["document_id"]]
                if requested_document_ids is None:
                    project_docs = await db.execute(
                        select(SourceDocument.id).where(
                            SourceDocument.project_id == project_id,
                            SourceDocument.status != "deleted",
                        )
                    )
                    requested_document_ids = [str(item[0]) for item in project_docs.all()]
                requested_documents = {str(item) for item in requested_document_ids}
                if quoted_documents != requested_documents:
                    raise ValueError("报价对应的文档范围与任务不一致，请重新获取报价")
                if task_type == "ocr" and len(quoted_documents) != 1:
                    raise ValueError("OCR 任务只能使用单个文档的报价")
                charge_status = "frozen"
            task = Task(
                project_id=project_id,
                user_id=resolved_user_id,
                billing_quote_id=quote_id,
                charge_status=charge_status,
                task_type=task_type,
                status="pending",
                progress=0,
                payload=safe_kwargs,
            )
            db.add(task)
            await db.flush()
            if quote_id:
                from app.models.platform import BillingQuote
                quote = await db.get(BillingQuote, quote_id)
                if quote:
                    quote.task_id = task.id
            await db.commit()
            await db.refresh(task)
            task_id = task.id

        self._start_task(task_id, task_type, coro_func, **safe_kwargs)
        return task_id
    async def update_progress(self, task_id: uuid.UUID, progress: int, meta: dict | None = None):
        """更新任务进度。meta 可包含 {current, total} 用于前端展示 m/N"""
        try:
            async with async_session() as db:
                now = datetime.now(timezone.utc)
                values = {
                    "progress": max(0, min(100, int(progress))),
                    "heartbeat_at": now,
                    "lease_expires_at": now + timedelta(seconds=max(30, settings.task_lease_seconds)),
                }
                if meta:
                    result = await db.execute(select(Task.result).where(Task.id == task_id))
                    current_result = result.scalar_one_or_none()
                    if isinstance(current_result, dict):
                        values["result"] = {**current_result, **meta}
                    else:
                        values["result"] = meta
                await db.execute(
                    update(Task).where(
                        Task.id == task_id,
                        Task.status == "running",
                        Task.worker_id == self.worker_id,
                    ).values(**values)
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

    async def cancel(self, task_id: uuid.UUID, reason: str = "用户取消") -> bool:
        """先持久化取消和退款，再中断当前进程中的执行任务。"""
        should_interrupt = False
        async with async_session() as db:
            task = await db.scalar(select(Task).where(Task.id == task_id).with_for_update())
            if task is None or task.status not in {"pending", "running"}:
                return False
            if task.billing_quote_id and task.charge_status == "frozen":
                await refund_quote(db, task.billing_quote_id, task.user_id, "任务取消退款")
                task.charge_status = "refunded"
            task.status = "cancelled"
            task.error = reason
            task.worker_id = None
            task.lease_expires_at = None
            await db.commit()
            atask = self._tasks.get(task_id)
            should_interrupt = atask is not None and not atask.done()
        if should_interrupt:
            atask.cancel()
        return True

    async def retry(self, task_id: uuid.UUID, max_attempts: int = 3) -> bool:
        """重新排队非计费失败任务；计费任务必须重新报价，避免绕过扣费。"""
        async with async_session() as db:
            task = await db.scalar(select(Task).where(Task.id == task_id).with_for_update())
            if task is None or task.status != "failed":
                return False
            if task.charge_status != "none":
                raise ValueError("已计费或已退款的任务不能直接重试，请重新报价")
            if task.attempt_count >= max_attempts:
                raise ValueError("任务重试次数已达上限")
            coro_func = self._task_registry().get(task.task_type)
            if coro_func is None or not task.payload:
                raise ValueError("任务缺少可恢复的执行参数")
            task.status = "pending"
            task.progress = 0
            task.error = None
            task.result = None
            task.worker_id = None
            task.lease_expires_at = None
            await db.commit()
            payload = dict(task.payload)
        self._start_task(task.id, task.task_type, coro_func, **payload)
        return True

    async def recover_on_startup(self):
        """恢复待处理任务以及已经失去租约的运行中任务。

        不能无条件接管所有 running 任务，否则多实例部署时新进程启动会
        抢走仍由旧进程正常执行的任务，造成重复调用和重复写结果。
        """
        registry = self._task_registry()
        recoverable: list[Task] = []
        now = datetime.now(timezone.utc)
        async with async_session() as db:
            result = await db.execute(
                select(Task).where(
                    (Task.status == "pending")
                    | (
                        (Task.status == "running")
                        & (
                            Task.lease_expires_at.is_(None)
                            | (Task.lease_expires_at < now)
                        )
                    )
                )
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

            await db.commit()

        for task in recoverable:
            payload = dict(task.payload or {})
            if task.task_type == "ocr":
                document_id = payload.get("document_id")
                if document_id:
                    async with async_session() as db:
                        await db.execute(
                            update(SourceDocument)
                            .where(
                                SourceDocument.id == uuid.UUID(str(document_id)),
                                SourceDocument.status.in_(["registered", "ocr_processing"]),
                            )
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
