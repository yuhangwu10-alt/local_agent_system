"""数据库 advisory lock 工具，带超时重试和僵尸事务自动清理。"""

import asyncio
import logging

import sqlalchemy as sa

from app.database import async_session

logger = logging.getLogger(__name__)

# 最大等待时间（秒），超过这个时间还没拿到锁就报错
DEFAULT_LOCK_TIMEOUT = 300  # 5 分钟，给大数据量写入留足时间
# 初始重试间隔（秒）
INITIAL_RETRY_INTERVAL = 2


async def acquire_advisory_lock_with_retry(
    db,
    lock_key: int,
    max_wait: int = DEFAULT_LOCK_TIMEOUT,
) -> None:
    """
    获取 transaction-level advisory lock，带自动重试和僵尸事务清理。

    流程：
    1. 先尝试非阻塞获取锁 (pg_try_advisory_xact_lock)
    2. 如果失败，用独立连接查找并终止持有该锁的僵尸事务（idle in transaction > 5分钟）
    3. 指数退避重试，直到拿到锁或超时

    这样即使有残留事务卡着锁，也能自动清理并继续写库，
    不会让 LLM 提取结果烂在内存里。
    """
    deadline = asyncio.get_event_loop().time() + max_wait
    retry_interval = INITIAL_RETRY_INTERVAL
    attempt = 0

    while True:
        attempt += 1

        # 非阻塞尝试
        result = await db.execute(
            sa.text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": lock_key}
        )
        if result.scalar():
            if attempt > 1:
                logger.info(f"advisory lock key={lock_key} 在第 {attempt} 次尝试时成功获取")
            return

        # 没拿到锁
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise RuntimeError(
                f"数据库写入锁获取超时（等待 {max_wait}s, 尝试 {attempt} 次），"
                f"请稍后重试。如反复出现请联系管理员检查数据库状态。"
            )

        logger.warning(
            f"advisory lock key={lock_key} 被占用，第 {attempt} 次尝试失败，"
            f"剩余等待 {remaining:.0f}s，正在清理僵尸事务..."
        )

        # 用独立连接清理僵尸事务
        killed = await _kill_zombie_lock_holders(lock_key)
        if killed:
            logger.warning(
                f"已终止 {killed} 个持有 advisory lock key={lock_key} 的僵尸事务，即将重试"
            )
            retry_interval = INITIAL_RETRY_INTERVAL  # 杀了僵尸就快速重试
        else:
            logger.info(
                f"未发现可清理的僵尸事务，{retry_interval:.0f}s 后重试..."
            )
            retry_interval = min(retry_interval * 1.5, 10)  # 指数退避，上限 10s

        await asyncio.sleep(min(retry_interval, remaining))


async def _kill_zombie_lock_holders(lock_key: int) -> int:
    """终止持有指定 advisory lock 且处于 idle in transaction 超过 5 分钟的僵尸连接。"""
    try:
        async with async_session() as cleanup_db:
            result = await cleanup_db.execute(
                sa.text("""
                    SELECT pg_terminate_backend(l.pid)
                    FROM pg_locks l
                    JOIN pg_stat_activity a ON l.pid = a.pid
                    WHERE l.locktype = 'advisory'
                      AND l.classid = 0
                      AND l.objid = :key
                      AND l.granted = true
                      AND a.state = 'idle in transaction'
                      AND a.pid <> pg_backend_pid()
                      AND a.query_start < now() - interval '5 minutes'
                """),
                {"key": lock_key},
            )
            await cleanup_db.commit()
            return result.scalar() or 0
    except Exception as e:
        logger.warning(f"清理僵尸事务失败 (key={lock_key}): {e}")
        return 0
