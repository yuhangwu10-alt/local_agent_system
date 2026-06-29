import asyncio
import json
import logging
import re

from sqlalchemy import select

from app.database import async_session
from app.models.page import PageContent
from app.models.project import SourceDocument
from app.services import llm_service
from app.services.task_manager import task_manager

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 100
DEFAULT_CONCURRENCY = 5
MAX_CHARS_PER_PAGE = 300

# ============================================================
# 提示词
# ============================================================

BATCH_KEYWORD_PROMPT = """你正在为方志专题"{topic_name}"寻找检索关键词。以下是底表部分页面内容（第 {page_start}-{page_end} 页）。

请找出这批页面中与"{topic_name}"相关的内容，提取可作为检索关键词的词汇。

## 要求
- 核心词：直接表示该专题的词，必须来自或贴近底表文本
- 扩展词：相关但间接的词，服务于后续在底表中检索页面
- 同时包含简体和繁体变体
- 只基于提供的页面文本，不要臆造

## 输出 JSON 格式（必须严格遵守）
{{
  "候选核心词": ["词1", "词2", "词3"],
  "候选扩展词": ["词1", "词2", "词3"],
  "相关页码": [页码1, 页码2],
  "内容摘要": "这批页面中与专题相关的主要内容概述（50-200字）"
}}

页面文本：
---
{page_texts}
---

只输出 JSON，不要其他文字。"""

CONSOLIDATION_KEYWORD_PROMPT = """综合以下各批次对专题"{topic_name}"的关键词分析结果，去重合并，给出最终的关键词和专题描述。

## 各批次分析结果

{batch_summaries}

## 输出 JSON 格式（必须严格遵守）
{{
  "核心词": ["词1", "词2", "词3"],
  "扩展词": ["词1", "词2", "词3"],
  "专题描述": "综合描述（100-300字），说明该专题在这份方志中的覆盖范围和证据基础"
}}

## 整合规则
1. 核心词 5-15 个，扩展词 5-15 个
2. 同时包含简体和繁体变体
3. 关键词必须来自底表文本中实际出现的词汇
4. 按重要性排序
5. 专题描述要整合各批次的发现

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
    match = re.search(r'\{[\s\S]*\}', response)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    raise ValueError(f"无法解析 JSON: {response[:300]}")


# ============================================================
# 核心流程
# ============================================================

async def _analyze_one_batch(
    batch_no: int,
    page_start: int,
    page_end: int,
    pages: list[PageContent],
    topic_name: str,
    llm_config: dict | None,
    sem: asyncio.Semaphore,
) -> dict:
    page_lines = []
    for page in pages:
        text = (page.content or "").strip()
        if not text:
            continue
        page_lines.append(f"第{page.page_no}页：{text[:MAX_CHARS_PER_PAGE]}")

    if not page_lines:
        return {"batch_no": batch_no, "候选核心词": [], "候选扩展词": [], "相关页码": [], "内容摘要": "", "error": None}

    prompt = BATCH_KEYWORD_PROMPT.format(
        topic_name=topic_name,
        page_start=page_start,
        page_end=page_end,
        page_texts="\n\n".join(page_lines),
    )

    try:
        async with sem:
            response = await llm_service.chat([
                {"role": "system", "content": f"你是一个中国方志专题分析助手。请为专题「{topic_name}」分析关键词。只输出 JSON。"},
                {"role": "user", "content": prompt},
            ], runtime_config=llm_config)
        parsed = _parse_json(response)
        return {
            "batch_no": batch_no,
            "候选核心词": parsed.get("候选核心词", []),
            "候选扩展词": parsed.get("候选扩展词", []),
            "相关页码": parsed.get("相关页码", []),
            "内容摘要": parsed.get("内容摘要", ""),
            "response": response,
            "error": None,
        }
    except Exception as e:
        logger.error(f"关键词批次 {batch_no} 失败: {e}")
        return {"batch_no": batch_no, "候选核心词": [], "候选扩展词": [], "相关页码": [], "内容摘要": "", "response": "", "error": str(e)}


async def _consolidate_keywords(
    batch_results: list[dict],
    topic_name: str,
    llm_config: dict | None,
) -> dict:
    summaries = []
    for br in batch_results:
        if br.get("error"):
            summaries.append(f"批次 {br['batch_no']}: 分析失败 - {br['error']}")
        else:
            summaries.append(
                f"批次 {br['batch_no']}（第{br.get('page_range','?')}页）：\n"
                f"核心词: {br.get('候选核心词',[])}\n"
                f"扩展词: {br.get('候选扩展词',[])}\n"
                f"摘要: {br.get('内容摘要','')}"
            )

    if not summaries:
        return {"核心词": [topic_name], "扩展词": [], "专题描述": f"围绕「{topic_name}」从方志页面集合中提取。"}

    prompt = CONSOLIDATION_KEYWORD_PROMPT.format(
        topic_name=topic_name,
        batch_summaries="\n\n---\n\n".join(summaries),
    )

    try:
        response = await llm_service.chat([
            {"role": "system", "content": f"你是一个中国方志专题整理助手。请为专题「{topic_name}」整合关键词。只输出 JSON。"},
            {"role": "user", "content": prompt},
        ], runtime_config=llm_config)
        parsed = _parse_json(response)
        return {
            "核心词": parsed.get("核心词", [topic_name]),
            "扩展词": parsed.get("扩展词", []),
            "专题描述": parsed.get("专题描述", f"围绕「{topic_name}」从方志页面集合中提取。"),
            "合并原文": response,
        }
    except Exception as e:
        logger.error(f"关键词汇总失败: {e}")
        all_core = []
        all_ext = []
        for br in batch_results:
            all_core.extend(br.get("候选核心词", []))
            all_ext.extend(br.get("候选扩展词", []))
        return {
            "核心词": list(dict.fromkeys(all_core))[:15] or [topic_name],
            "扩展词": list(dict.fromkeys(all_ext))[:15],
            "专题描述": f"围绕「{topic_name}」从方志页面集合中提取。",
            "合并方式": "简单去重（汇总 LLM 调用失败）",
        }


async def run_keyword_completion(
    task_id,
    proj_id: str,
    document_id: str,
    topic_name: str,
    llm_config: dict | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    llm_concurrency: int = DEFAULT_CONCURRENCY,
    **kwargs,
) -> dict:
    from uuid import UUID

    t_id = UUID(str(task_id))
    doc_id = UUID(str(document_id))


    async with async_session() as db:
        result = await db.execute(
            select(PageContent)
            .where(
                PageContent.document_id == doc_id,
                PageContent.ocr_status == "completed",
                PageContent.content.isnot(None),
                PageContent.content != "",
            )
            .order_by(PageContent.page_no)
        )
        pages = result.scalars().all()

    if not pages:
        raise ValueError("没有已 OCR 的页面，请先完成 OCR 处理")

    total = len(pages)
    batch_size = max(10, min(500, batch_size))

    batches = []
    for i in range(0, total, batch_size):
        batch_pages = pages[i:i + batch_size]
        batches.append({
            "batch_no": len(batches) + 1,
            "page_start": batch_pages[0].page_no,
            "page_end": batch_pages[-1].page_no,
            "pages": batch_pages,
        })

    await task_manager.update_progress(t_id, 5, {"current": 0, "total": total, "type": "keyword"})
    sem = asyncio.Semaphore(max(1, int(llm_concurrency or DEFAULT_CONCURRENCY)))

    async def _process(b: dict) -> dict:
        br = await _analyze_one_batch(
            batch_no=b["batch_no"],
            page_start=b["page_start"],
            page_end=b["page_end"],
            pages=b["pages"],
            topic_name=topic_name,
            llm_config=llm_config,
            sem=sem,
        )
        br["page_range"] = f"{b['page_start']}-{b['page_end']}"
        br["page_count"] = len(b["pages"])
        return br

    futures = [asyncio.create_task(_process(b)) for b in batches]
    batch_results = []
    completed = 0
    try:
        for coro in asyncio.as_completed(futures):
            br = await coro
            batch_results.append(br)
            completed += 1
            progress = int(completed / len(batches) * 80) + 5
            await task_manager.update_progress(t_id, progress, {"current": min(completed * batch_size, total), "total": total, "type": "keyword"})
    except asyncio.CancelledError:
        for task in futures:
            task.cancel()
        await asyncio.gather(*futures, return_exceptions=True)
        raise

    batch_results.sort(key=lambda x: x["batch_no"])
    await task_manager.update_progress(t_id, 85, {"current": total, "total": total, "type": "keyword"})

    consolidated = await _consolidate_keywords(batch_results, topic_name, llm_config)
    await task_manager.update_progress(t_id, 95, {"current": total, "total": total, "type": "keyword"})

    return {
        "专题名称": topic_name,
        "文件ID": str(doc_id),
        "总页数": total,
        "批次数量": len(batches),
        "关键词": {
            "核心词": consolidated.get("核心词", []),
            "扩展词": consolidated.get("扩展词", []),
        },
        "专题描述": consolidated.get("专题描述", ""),
        "批次结果": [
            {
                "批次号": br["batch_no"],
                "页码范围": br.get("page_range", ""),
                "候选核心词": br.get("候选核心词", []),
                "候选扩展词": br.get("候选扩展词", []),
                "状态": "失败" if br.get("error") else "成功",
            }
            for br in batch_results
        ],
    }
