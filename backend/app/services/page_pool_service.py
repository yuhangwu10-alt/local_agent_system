import asyncio
import json
import logging
import re
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import func, select, update

from app.database import async_session
from app.models.page import PageContent, PagePool
from app.models.project import SourceDocument
from app.models.theme import ThemeConfig
from app.services import llm_service
from app.services.task_manager import task_manager

logger = logging.getLogger(__name__)

MIN_TOPIC_SCORE = 40
LLM_SCORE_BATCH_SIZE = 10          # 每批 10 页打包调一次 LLM
DEFAULT_SCORE_CONCURRENCY = 5
MAX_CHARS_PER_PAGE = 500           # 每页最多取 500 字给 LLM
MAX_THEMES_PER_BATCH = 8

# 评分前直接跳过的页面分类
SKIP_CATEGORIES = {"非正文污染页", "图题图版页", "编修名录页", "出版说明_版权信息"}

# ============================================================
# LLM 批量评分提示词
# ============================================================

LLM_SCORING_PROMPT = """你是中国方志页面评分助手。专题：「{theme}」
专题描述：{description}
核心关键词：{core_keywords}
扩展关键词：{extended_keywords}
页面池对象：{page_pool_objects}
可抽取单元：{extractable_units}
可能回答的问题：{research_questions}

请为以下每页评估与专题的相关度（0-100 分）。

## 评分标准
- 90-100：核心页面，直接讨论该专题的核心内容
- 70-89：高度相关，包含专题关键内容或重要证据
- 40-69：部分相关，涉及专题相关内容但非主要讨论对象
- 20-39：弱相关，仅提到专题相关的个别词汇
- 0-19：不相关

## 每页信息包含：页码、分类、质量等级、文本内容（截断到 {max_chars} 字）

## 页面列表
{page_list}

## 输出 JSON 格式（必须严格遵守）
{{
  "评分列表": [
    {{
      "page_id": "页面ID",
      "page_no": 页码,
      "分数": 0-100,
      "等级": "core|borderline|excluded",
      "理由": "简短理由（20字以内）"
    }}
  ]
}}

只输出 JSON，不要其他文字。"""

MULTI_THEME_SCORING_PROMPT = """你是中国方志多专题页面池评分助手。

你将同时评估一批页面与多个研究专题的相关度。请只输出与页面相关且分数 >= 40 的专题；分数低于 40 的专题不要输出。

## 待评估专题
{theme_list}

## 评分标准
- 90-100：核心页面，直接讨论该专题的核心内容
- 70-89：高度相关，包含专题关键内容或重要证据
- 40-69：部分相关，涉及专题相关内容但非主要讨论对象
- 0-39：不输出

## 判断规则
- 同一页可以同时属于多个专题
- 专题名称必须严格使用上方“待评估专题”中的名称
- 不要为了覆盖专题而强行匹配；无关页面可以不输出
- 理由要简短，说明具体命中文本依据

## 页面列表
{page_list}

## 输出 JSON 格式（必须严格遵守）
{{
  "评分列表": [
    {{
      "page_no": 47,
      "入选专题": [
        {{
          "专题名称": "专题名称",
          "分数": 92,
          "理由": "记载金花堰、灌溉田亩"
        }}
      ]
    }}
  ]
}}

只输出 JSON，不要其他文字。"""


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
    match = re.search(r'\{[\s\S]*"评分列表"[\s\S]*\}', response)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    raise ValueError(f"无法解析评分 JSON: {response[:300]}")


def _normalize_keywords_config(value) -> dict:
    if isinstance(value, list):
        return {"核心词": value, "扩展词": []}
    if isinstance(value, dict):
        return value
    return {}


def _coerce_text_list(value, limit: int = 30) -> list[str]:
    if isinstance(value, list):
        source = value
    elif value is None:
        source = []
    else:
        source = re.split(r"[、，,；;\n\r]+", str(value))
    result: list[str] = []
    seen: set[str] = set()
    for item in source:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text[:120])
        if len(result) >= limit:
            break
    return result


def _score_level(score: int) -> str:
    return "core" if score >= 70 else "borderline"


def _score_confidence(score: int) -> str:
    return "high" if score >= 70 else "medium" if score >= 40 else "low"


def _page_prompt_entry(page: PageContent) -> str:
    cls = page.classification or {}
    text = (page.content or "")[:MAX_CHARS_PER_PAGE]
    material = cls.get("材料分类") or "未分类"
    quality = cls.get("质量等级") or "未评级"
    usage = (cls.get("推荐用途") or "未预判")[:10]
    return (
        f"第{page.page_no}页 "
        f"分类:{material} "
        f"质量:{quality} "
        f"用途:{usage}\n"
        f"文本:{text}"
    )


async def _load_scorable_pages(project_id) -> tuple[list[PageContent], int, int]:
    async with async_session() as db:
        pages_result = await db.execute(
            select(PageContent)
            .join(SourceDocument, PageContent.document_id == SourceDocument.id)
            .where(
                SourceDocument.project_id == project_id,
                PageContent.ocr_status == "completed",
                PageContent.content.isnot(None),
                PageContent.content != "",
            )
            .order_by(SourceDocument.created_at, PageContent.page_no)
        )
        pages = pages_result.scalars().all()

    scorable = []
    skipped = 0
    for page in pages:
        cls = page.classification or {}
        cat = cls.get("材料分类", "")
        if cat in SKIP_CATEGORIES:
            skipped += 1
            continue
        scorable.append(page)
    return scorable, len(pages), skipped


async def _write_page_pool_entries(
    theme_entries: dict[UUID, list[dict]],
    theme_meta: dict[UUID, dict],
    page_map: dict[str, PageContent],
) -> dict[UUID, dict]:
    results: dict[UUID, dict] = {}
    async with async_session() as db:
        for theme_id, entries in theme_entries.items():
            meta = theme_meta[theme_id]
            deduped: dict[UUID, dict] = {}
            for entry in entries:
                page_obj = page_map.get(str(entry.get("page_id", "")))
                if page_obj is None:
                    continue
                current = deduped.get(page_obj.id)
                if current is None or int(entry["score"]) > int(current["score"]):
                    deduped[page_obj.id] = entry

            if not deduped:
                results[theme_id] = {
                    "专题ID": str(theme_id),
                    "专题名称": meta["name"],
                    "入选页数": 0,
                    "generation": 0,
                    "提示": "无符合条件的页面，保留旧结果",
                }
                continue

            lock_key = int(theme_id.int % (2**31))
            from app.utils.db_lock import acquire_advisory_lock_with_retry
            await acquire_advisory_lock_with_retry(db, lock_key)
            gen_result = await db.execute(
                select(func.max(PagePool.generation)).where(PagePool.theme_id == theme_id)
            )
            current_gen = gen_result.scalar() or 0
            new_gen = current_gen + 1

            for entry in deduped.values():
                score = int(entry["score"])
                page_obj = page_map[str(entry["page_id"])]
                db.add(
                    PagePool(
                        theme_id=theme_id,
                        page_id=page_obj.id,
                        score=score,
                        relevance_level=_score_level(score),
                        reason=str(entry.get("reason") or f"LLM 评分: {score}"),
                        命中关键词=meta["core_keywords"][:5],
                        命中规则={"LLM语义评分": score, "理由": entry.get("reason") or ""},
                        关键词命中数=0,
                        信号命中数=0,
                        置信度=_score_confidence(score),
                        generation=new_gen,
                        is_latest=True,
                        is_manual=False,
                    )
                )

            await db.execute(
                update(PagePool)
                .where(
                    PagePool.theme_id == theme_id,
                    PagePool.is_latest == True,
                    PagePool.generation < new_gen,
                )
                .values(is_latest=False)
            )
            results[theme_id] = {
                "专题ID": str(theme_id),
                "专题名称": meta["name"],
                "入选页数": len(deduped),
                "generation": new_gen,
            }

        await db.commit()
    return results


# ============================================================
# 单批 LLM 评分
# ============================================================

async def _score_one_batch(
    pages: list[PageContent],
    theme_name: str,
    description: str,
    core_keywords: list[str],
    extended_keywords: list[str],
    page_pool_objects: list[str],
    extractable_units: list[str],
    research_questions: list[str],
    llm_config: dict | None,
    sem: asyncio.Semaphore,
) -> list[dict]:
    """对一批页面调用 LLM 评分，返回评分列表"""
    # 构建页面列表文本
    page_entries = []
    for page in pages:
        page_entries.append(_page_prompt_entry(page))

    prompt = LLM_SCORING_PROMPT.format(
        theme=theme_name,
        description=description,
        core_keywords=", ".join(core_keywords[:10]),
        extended_keywords=", ".join(extended_keywords[:10]),
        page_pool_objects=", ".join(page_pool_objects[:12]) or "未指定",
        extractable_units=", ".join(extractable_units[:12]) or "未指定",
        research_questions="；".join(research_questions[:5]) or "未指定",
        max_chars=MAX_CHARS_PER_PAGE,
        page_list="\n\n---\n\n".join(page_entries),
    )

    try:
        async with sem:
            response = await llm_service.chat([
                {"role": "system", "content": "你是一个中国方志页面评分助手。请只输出 JSON，不要其他文字。"},
                {"role": "user", "content": prompt},
            ], runtime_config=llm_config)
        parsed = _parse_json(response)
        return parsed.get("评分列表", [])
    except Exception as e:
        logger.error(f"LLM 页面评分批次失败: {e}")
        # 失败时返回全部排除
        return [
            {"page_id": str(page.id), "page_no": page.page_no, "分数": 0, "等级": "excluded", "理由": f"评分失败: {e}"}
            for page in pages
        ]


async def _score_multi_theme_batch(
    pages: list[PageContent],
    themes: list[dict],
    llm_config: dict | None,
    sem: asyncio.Semaphore,
) -> list[dict]:
    page_entries = [_page_prompt_entry(page) for page in pages]
    theme_lines = []
    for idx, theme in enumerate(themes, start=1):
        theme_lines.append(
            "\n".join(
                [
                    f"{idx}. 专题名称：{theme['name']}",
                    f"   专题描述：{theme['description']}",
                    f"   核心关键词：{'、'.join(theme['core_keywords'][:10])}",
                    f"   扩展关键词：{'、'.join(theme['extended_keywords'][:10])}",
                    f"   页面池对象：{'、'.join(theme['page_pool_objects'][:12]) or '未指定'}",
                    f"   可抽取单元：{'、'.join(theme['extractable_units'][:12]) or '未指定'}",
                    f"   可能回答的问题：{'；'.join(theme['research_questions'][:5]) or '未指定'}",
                ]
            )
        )

    prompt = MULTI_THEME_SCORING_PROMPT.format(
        theme_list="\n\n".join(theme_lines),
        page_list="\n\n---\n\n".join(page_entries),
    )

    try:
        async with sem:
            response = await llm_service.chat(
                [
                    {"role": "system", "content": "你是一个中国方志多专题页面池评分助手。请只输出 JSON，不要其他文字。"},
                    {"role": "user", "content": prompt},
                ],
                runtime_config=llm_config,
            )
        parsed = _parse_json(response)
        return parsed.get("评分列表", [])
    except Exception as e:
        logger.error(f"LLM 多专题页面评分批次失败: {e}")
        return []


def _coerce_score(value) -> int:
    try:
        score = int(float(value))
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, score))


def _normalize_selected_topics(value) -> list[dict]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


# ============================================================
# 主流程
# ============================================================

async def run_page_pool_generation(task_id, theme_id, **kwargs):
    """页面池生成异步任务（LLM 语义评分替代规则打分）"""
    t_id = UUID(str(task_id))
    th_id = UUID(str(theme_id))

    llm_config = kwargs.get("llm_config")
    llm_concurrency = max(1, int(kwargs.get("llm_concurrency", DEFAULT_SCORE_CONCURRENCY) or DEFAULT_SCORE_CONCURRENCY))

    async with async_session() as db:
        result = await db.execute(select(ThemeConfig).where(ThemeConfig.id == th_id))
        theme = result.scalar_one_or_none()
        if theme is None:
            raise ValueError(f"专题不存在: {th_id}")
        keywords_config = _normalize_keywords_config(theme.keywords)
        project_id = theme.project_id

    core_keywords = _coerce_text_list(keywords_config.get("核心词"), 40)
    extended_keywords = _coerce_text_list(keywords_config.get("扩展词"), 40)
    page_pool_objects = _coerce_text_list(keywords_config.get("页面池对象"), 24)
    extractable_units = _coerce_text_list(keywords_config.get("可抽取单元"), 24)
    research_questions = _coerce_text_list(keywords_config.get("可能回答的问题"), 8)
    description = theme.description or ""

    if not core_keywords:
        raise ValueError("未配置核心关键词，请先在专题配置中设置关键词")

    scorable, total_pages, skipped = await _load_scorable_pages(project_id)
    if not scorable:
        return {"专题ID": str(th_id), "入选页数": 0, "generation": 0}

    logger.info(f"页面池 LLM 评分: 专题={theme.theme}, 总页={total_pages}, 跳过={skipped}, 待评分={len(scorable)}")

    batches = [scorable[i:i + LLM_SCORE_BATCH_SIZE] for i in range(0, len(scorable), LLM_SCORE_BATCH_SIZE)]

    await task_manager.update_progress(t_id, 5, {"current": 0, "total": len(scorable), "type": "pool"})
    sem = asyncio.Semaphore(llm_concurrency)

    async def _process(pages_batch: list[PageContent]) -> list[dict]:
        return await _score_one_batch(
            pages=pages_batch,
            theme_name=theme.theme,
            description=description,
            core_keywords=core_keywords,
            extended_keywords=extended_keywords,
            page_pool_objects=page_pool_objects,
            extractable_units=extractable_units,
            research_questions=research_questions,
            llm_config=llm_config,
            sem=sem,
        )

    futures = [asyncio.create_task(_process(b)) for b in batches]
    all_scores = []
    completed = 0
    try:
        for coro in asyncio.as_completed(futures):
            batch_scores = await coro
            all_scores.extend(batch_scores)
            completed += 1
            progress = int(completed / len(batches) * 90) + 5
            await task_manager.update_progress(t_id, progress, {"current": min(completed * LLM_SCORE_BATCH_SIZE, len(scorable)), "total": len(scorable), "type": "pool"})
    except asyncio.CancelledError:
        for task in futures:
            task.cancel()
        await asyncio.gather(*futures, return_exceptions=True)
        raise

    page_by_id = {str(p.id): p for p in scorable}

    theme_meta = {
        th_id: {
            "name": theme.theme,
            "core_keywords": core_keywords,
        }
    }
    theme_entries: dict[UUID, list[dict]] = {th_id: []}
    for s in all_scores:
        score = int(s.get("分数", 0))
        if score >= MIN_TOPIC_SCORE:
            pid = s.get("page_id", "")
            reason = s.get("理由", f"LLM 语义评分: {score}")
            page_obj = page_by_id.get(pid)
            if page_obj is None:
                pno = s.get("page_no", 0)
                for pp in scorable:
                    if pp.page_no == pno:
                        page_obj = pp
                        pid = str(pp.id)
                        break

            if page_obj is not None:
                theme_entries[th_id].append({
                    "page_id": str(page_obj.id),
                    "score": score,
                    "reason": reason,
                })

    write_results = await _write_page_pool_entries(theme_entries, theme_meta, page_by_id)
    result = write_results[th_id]
    result.update({"总页数": total_pages, "预筛选跳过": skipped, "LLM评分页数": len(all_scores)})
    return result


async def run_multi_theme_page_pool_generation(task_id, theme_ids, **kwargs):
    """多专题联合页面池生成任务。

    一次把多个专题放入同一轮页面评分 prompt，模型只返回分数 >= 40 的
    page/topic 命中；随后仍按专题分别写入现有 PagePool 表。
    """
    t_id = UUID(str(task_id))
    th_ids = []
    for theme_id in theme_ids or []:
        parsed_id = UUID(str(theme_id))
        if parsed_id not in th_ids:
            th_ids.append(parsed_id)
    if not th_ids:
        raise ValueError("未选择要生成页面池的专题")
    if len(th_ids) > MAX_THEMES_PER_BATCH:
        raise ValueError(f"一次最多联合评估 {MAX_THEMES_PER_BATCH} 个专题")

    llm_config = kwargs.get("llm_config")
    llm_concurrency = max(1, int(kwargs.get("llm_concurrency", DEFAULT_SCORE_CONCURRENCY) or DEFAULT_SCORE_CONCURRENCY))

    async with async_session() as db:
        result = await db.execute(select(ThemeConfig).where(ThemeConfig.id.in_(th_ids)))
        theme_rows = result.scalars().all()

    themes_by_id = {theme.id: theme for theme in theme_rows}
    missing = [str(theme_id) for theme_id in th_ids if theme_id not in themes_by_id]
    if missing:
        raise ValueError(f"专题不存在: {', '.join(missing)}")

    project_ids = {theme.project_id for theme in theme_rows}
    if len(project_ids) != 1:
        raise ValueError("联合页面池评分要求所有专题属于同一个项目")
    project_id = next(iter(project_ids))

    themes: list[dict] = []
    theme_meta: dict[UUID, dict] = {}
    theme_entries: dict[UUID, list[dict]] = {}
    theme_name_to_id: dict[str, UUID] = {}
    for th_id in th_ids:
        theme = themes_by_id[th_id]
        keywords_config = _normalize_keywords_config(theme.keywords)
        core_keywords = _coerce_text_list(keywords_config.get("核心词"), 40) or [theme.theme]
        extended_keywords = _coerce_text_list(keywords_config.get("扩展词"), 40)
        page_pool_objects = _coerce_text_list(keywords_config.get("页面池对象"), 24)
        extractable_units = _coerce_text_list(keywords_config.get("可抽取单元"), 24)
        research_questions = _coerce_text_list(keywords_config.get("可能回答的问题"), 8)
        item = {
            "id": th_id,
            "name": theme.theme,
            "description": theme.description or "",
            "core_keywords": core_keywords,
            "extended_keywords": extended_keywords,
            "page_pool_objects": page_pool_objects,
            "extractable_units": extractable_units,
            "research_questions": research_questions,
        }
        themes.append(item)
        theme_meta[th_id] = {"name": theme.theme, "core_keywords": core_keywords}
        theme_entries[th_id] = []
        theme_name_to_id[theme.theme.strip()] = th_id

    scorable, total_pages, skipped = await _load_scorable_pages(project_id)
    if not scorable:
        return {
            "专题数量": len(themes),
            "总页数": total_pages,
            "预筛选跳过": skipped,
            "LLM评分页数": 0,
            "总入选页数": 0,
            "专题结果": [
                {
                    "专题ID": str(theme["id"]),
                    "专题名称": theme["name"],
                    "入选页数": 0,
                    "generation": 0,
                    "提示": "无可评分页面",
                }
                for theme in themes
            ],
        }

    logger.info(
        "多专题页面池 LLM 联合评分: 专题数=%s, 总页=%s, 跳过=%s, 待评分=%s",
        len(themes), total_pages, skipped, len(scorable),
    )

    batches = [scorable[i:i + LLM_SCORE_BATCH_SIZE] for i in range(0, len(scorable), LLM_SCORE_BATCH_SIZE)]
    await task_manager.update_progress(
        t_id,
        5,
        {"current": 0, "total": len(scorable), "type": "pool", "message": "联合页面池评分准备中"},
    )
    sem = asyncio.Semaphore(llm_concurrency)

    async def _process(pages_batch: list[PageContent]) -> list[dict]:
        return await _score_multi_theme_batch(
            pages=pages_batch,
            themes=themes,
            llm_config=llm_config,
            sem=sem,
        )

    futures = [asyncio.create_task(_process(batch)) for batch in batches]
    all_rows: list[dict] = []
    completed = 0
    try:
        for coro in asyncio.as_completed(futures):
            batch_rows = await coro
            all_rows.extend(batch_rows)
            completed += 1
            progress = int(completed / len(batches) * 90) + 5
            await task_manager.update_progress(
                t_id,
                progress,
                {
                    "current": min(completed * LLM_SCORE_BATCH_SIZE, len(scorable)),
                    "total": len(scorable),
                    "type": "pool",
                    "message": f"联合页面池评分 {completed}/{len(batches)} 批",
                },
            )
    except asyncio.CancelledError:
        for task in futures:
            task.cancel()
        await asyncio.gather(*futures, return_exceptions=True)
        raise

    page_by_id = {str(page.id): page for page in scorable}
    page_by_no: dict[int, PageContent] = {}
    for page in scorable:
        page_by_no.setdefault(page.page_no, page)

    matched_count = 0
    for row in all_rows:
        if not isinstance(row, dict):
            continue
        page_no = row.get("page_no", row.get("页码"))
        page_obj = None
        try:
            if page_no is not None:
                page_obj = page_by_no.get(int(page_no))
        except (TypeError, ValueError):
            page_obj = None
        if page_obj is None:
            continue

        selected_topics = _normalize_selected_topics(
            row.get("入选专题")
            or row.get("相关专题")
            or row.get("topics")
            or row.get("themes")
        )
        for selected in selected_topics:
            name = str(
                selected.get("专题名称")
                or selected.get("topic")
                or selected.get("theme")
                or ""
            ).strip()
            theme_id = theme_name_to_id.get(name)
            if theme_id is None:
                continue
            score = _coerce_score(selected.get("分数", selected.get("score")))
            if score < MIN_TOPIC_SCORE:
                continue
            matched_count += 1
            reason = selected.get("理由") or selected.get("reason") or f"LLM 语义评分: {score}"
            theme_entries[theme_id].append(
                {
                    "page_id": str(page_obj.id),
                    "score": score,
                    "reason": str(reason),
                }
            )

    write_results = await _write_page_pool_entries(theme_entries, theme_meta, page_by_id)
    theme_results = [write_results[theme["id"]] for theme in themes]
    total_selected = sum(int(result.get("入选页数", 0)) for result in theme_results)
    return {
        "专题数量": len(themes),
        "总页数": total_pages,
        "预筛选跳过": skipped,
        "LLM评分页数": len(all_rows),
        "LLM命中专题次数": matched_count,
        "总入选页数": total_selected,
        "专题结果": theme_results,
    }
