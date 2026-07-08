import asyncio
import json
import logging
import re
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import func, select, update

from app.database import async_session
from app.models.page import PageContent, PagePool
from app.models.narrative import NarrativeUnit
from app.models.theme import ThemeConfig
from app.services import llm_service
from app.services.task_manager import task_manager

logger = logging.getLogger(__name__)

NARRATIVE_BATCH_SIZE = 5          # 每批 5 页
DEFAULT_NARRATIVE_CONCURRENCY = 5
MAX_CHARS_PER_PAGE = 1500

# ============================================================
# LLM 叙事提取提示词
# ============================================================

NARRATIVE_EXTRACTION_PROMPT = """你是中国地方志结构化提取助手。

专题：「{theme}」
专题描述：{description}
叙事字段：{schema_fields}

请从以下页面中提取所有与「{theme}」直接相关的叙事单元。

## 要求
- 每页可提取 0 到多个单元
- 只提取明确与「{theme}」相关的内容，不要泛化
- 每个字段尽可能从原文中提取具体信息，不要留空
- 「原文证据」字段尽量引用原文原句（100-300 字）
- 必须严格使用下方 JSON 模板里的中文字段名，不得新增、删除或改名
- 禁止输出英文 key，例如 time、event、place、source、subject、evidence、keywords、unit_type、source_page、sourcePage、title、type、object、location
- 禁止输出繁体或混合字段名，例如「原文證據」「原文evidence」「叙事单元栏目」
- 信息缺失时使用「未知」，不要为了表达缺失而新增其他字段
- 「来源页码」只能填写页面列表中出现的页码数字；不要填写 0、空值或自造页码

## 页面列表
{pages_text}

## 输出 JSON 格式（必须严格遵守）
{{
  "叙事单元": [
    {{
      "来源页码": 页码数字,
      {schema_template}
      "原文证据": "引用原文..."
    }}
  ]
}}

最终输出只能包含一个 JSON 对象，顶层只能有「叙事单元」一个 key。不要输出 Markdown、解释文字或代码块。"""


# ============================================================
# JSON 解析
# ============================================================

def _parse_json(response: str) -> dict:
    try:
        return json.loads(response.strip())
    except json.JSONDecodeError:
        pass
    match = re.search(r'```(?:json)?\s*([\s\S]*?)```', response)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass
    match = re.search(r'\{[\s\S]*"叙事单元"[\s\S]*\}', response)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    raise ValueError(f"无法解析叙事 JSON: {response[:300]}")


# ============================================================
# 单批 LLM 叙事提取
# ============================================================

async def _extract_one_batch(
    pages: list[tuple[PagePool, PageContent]],
    theme_name: str,
    description: str,
    schema_fields: list[str],
    llm_config: dict | None,
    sem: asyncio.Semaphore,
) -> list[dict]:
    """从一批页面中提取叙事单元"""
    # 构建 schema 模板（除来源页码和原文证据外的字段）
    field_template = ",\n      ".join(f'"{f}": "..."' for f in schema_fields if f not in ("来源页码", "原文证据"))

    # 构建页面文本
    page_entries = []
    for pool_entry, page in pages:
        cls = page.classification or {}
        text = (page.content or "")[:MAX_CHARS_PER_PAGE]
        entry = (
            f"第{page.page_no}页（分类:{cls.get('材料分类','未知')} 质量:{cls.get('质量等级','D')}）:\n{text}"
        )
        page_entries.append(entry)

    prompt = NARRATIVE_EXTRACTION_PROMPT.format(
        theme=theme_name,
        description=description or f"围绕「{theme_name}」的相关内容",
        schema_fields=", ".join(schema_fields),
        schema_template=field_template,
        pages_text="\n\n---\n\n".join(page_entries),
    )

    try:
        async with sem:
            response = await llm_service.chat([
                {"role": "system", "content": f"你是中国地方志结构化提取助手。请从页面中提取与「{theme_name}」相关的叙事单元。必须严格使用用户给出的中文 JSON 字段名，禁止英文 key、繁体别名和额外字段，只输出 JSON。"},
                {"role": "user", "content": prompt},
            ], runtime_config=llm_config)
        parsed = _parse_json(response)
        units = parsed.get("叙事单元", [])
        # 补充 page_id 映射
        page_no_to_score = {page.page_no: pool_entry.score for pool_entry, page in pages}
        for unit in units:
            pno = unit.get("来源页码", 0)
            unit["_confidence"] = page_no_to_score.get(pno, 0)
        return units
    except Exception as e:
        logger.error(f"LLM 叙事提取批次失败: {e}")
        return []


# ============================================================
# 主流程
# ============================================================

async def run_narrative_extraction(task_id, theme_id, **kwargs):
    """叙事单元抽取异步任务（统一 LLM 语义提取）"""
    t_id = UUID(str(task_id))
    th_id = UUID(str(theme_id))

    llm_config = kwargs.get("llm_config")
    llm_concurrency = max(1, int(kwargs.get("llm_concurrency", DEFAULT_NARRATIVE_CONCURRENCY) or DEFAULT_NARRATIVE_CONCURRENCY))


    # 读取专题配置
    async with async_session() as db:
        theme_result = await db.execute(select(ThemeConfig).where(ThemeConfig.id == th_id))
        theme = theme_result.scalar_one_or_none()
        if theme is None:
            raise ValueError(f"专题不存在: {th_id}")

        description = theme.description or ""
        schema_fields = theme.narrative_schema or [
            "专题名称", "叙事单元标题", "单元类型", "涉及对象",
            "时间线索", "地点线索", "事件或行为", "关键词命中", "来源页码", "原文证据"
        ]

    # 获取页面池中 core + borderline 的页面
    async with async_session() as db:
        pool_result = await db.execute(
            select(PagePool, PageContent)
            .join(PageContent, PagePool.page_id == PageContent.id)
            .where(
                PagePool.theme_id == th_id,
                PagePool.is_latest == True,
                PagePool.relevance_level.in_(["core", "borderline"]),
            )
            .order_by(PagePool.score.desc())
        )
        pool_pages = pool_result.all()

    if not pool_pages:
        return {"专题ID": str(th_id), "叙事单元数": 0, "generation": 0}

    total = len(pool_pages)

    # 分批
    batches = []
    for i in range(0, total, NARRATIVE_BATCH_SIZE):
        batches.append(pool_pages[i:i + NARRATIVE_BATCH_SIZE])

    await task_manager.update_progress(t_id, 5, {"current": 0, "total": total, "type": "narrative"})
    # 并发提取
    sem = asyncio.Semaphore(llm_concurrency)

    async def _process(batch_pages):
        return await _extract_one_batch(
            pages=batch_pages,
            theme_name=theme.theme,
            description=description,
            schema_fields=schema_fields,
            llm_config=llm_config,
            sem=sem,
        )

    futures = [asyncio.create_task(_process(b)) for b in batches]
    all_units = []
    completed = 0
    try:
        for coro in asyncio.as_completed(futures):
            batch_units = await coro
            all_units.extend(batch_units)
            completed += 1
            progress = int(completed / len(batches) * 90) + 5
            await task_manager.update_progress(t_id, progress, {"current": min(completed * NARRATIVE_BATCH_SIZE, total), "total": total, "type": "narrative"})
    except asyncio.CancelledError:
        for task in futures:
            task.cancel()
        await asyncio.gather(*futures, return_exceptions=True)
        raise

    # BE-010: 空结果不覆盖旧数据
    if not all_units:
        return {"专题ID": str(th_id), "叙事单元数": 0, "generation": 0, "提示": "未提取到叙事单元，保留旧结果"}

    # 锁 + 读 gen + 写数据 + 翻转旧记录 — 全部在同一个 Session/事务内
    async with async_session() as db:
        lock_key = int(th_id.int % (2**31))
        from app.utils.db_lock import acquire_advisory_lock_with_retry
        await acquire_advisory_lock_with_retry(db, lock_key)

        gen_result = await db.execute(
            select(func.max(NarrativeUnit.generation)).where(NarrativeUnit.theme_id == th_id)
        )
        current_gen = gen_result.scalar() or 0
        new_gen = current_gen + 1

        for unit_data in all_units:
            try:
                pno = int(unit_data.get("来源页码", 0))
            except (ValueError, TypeError):
                pno = 0
            confidence = unit_data.pop("_confidence", 0)

            # 构建 fields dict（排除内部字段）
            fields = {k: v for k, v in unit_data.items() if not k.startswith("_")}

            unit = NarrativeUnit(
                theme_id=th_id,
                source_page=pno,
                fields=fields,
                confidence=confidence,
                generation=new_gen,
                is_latest=True,
                is_manual=False,
            )
            db.add(unit)

        await db.execute(
            update(NarrativeUnit)
            .where(
                NarrativeUnit.theme_id == th_id,
                NarrativeUnit.is_latest == True,
                NarrativeUnit.generation < new_gen,
            )
            .values(is_latest=False)
        )
        await db.commit()

    return {"专题ID": str(th_id), "叙事单元数": len(all_units), "generation": new_gen}
