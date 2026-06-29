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

BATCH_EXTRACTION_PROMPT = """你是一个严谨的中国地方志专题发现助手。你正在分析一批古籍 OCR 页面，任务是发现潜在的研究专题。

## 输出 JSON 格式（必须严格遵守）
{{
  "专题列表": [
    {{
      "专题名称": "专题名称（2-8字）",
      "专题描述": "该专题在这批页面中的具体表现和证据概述（50-200字）",
      "核心词": ["关键词1", "关键词2", "关键词3"],
      "扩展词": ["扩展词1", "扩展词2", "扩展词3"],
      "页面池对象": ["用于筛选页面池的对象或页面类型1", "对象2"],
      "可抽取单元": ["后续叙事单元表中适合抽取的字段1", "字段2"],
      "可能回答的问题": ["该专题可以回答的研究问题1"],
      "证据页码": [页码1, 页码2],
      "佐证摘录": [
        {{"页码": 页码1, "原文": "可支撑该专题的原文短句"}},
        {{"页码": 页码2, "原文": "可支撑该专题的原文短句"}}
      ]
    }}
  ]
}}

## 要求
- 只基于提供的页面文本发现专题，不要套用固定模板
- 核心词：直接从文本中出现的概念，同时包含简体和繁体
- 扩展词：相关但间接的概念，用于后续文本检索
- 页面池对象：用于后续页面池评分的页面类型、对象、栏目或证据载体，必须贴近文本
- 可抽取单元：该专题后续叙事单元表适合抽取的字段，使用短字段名，不要写成长句
- 可能回答的问题：该专题能够支持的研究问题，必须由本批页面证据支撑
- 专题名称要具体，避免过于宽泛（如"历史"、"文化"）
- 证据页码和佐证摘录必须来自本批次提供的页面文本，不能编造；每个专题提供 1-5 条即可
- 如果这批页面没有明显专题，返回空的专题列表即可
- 关注方志特有的专题类型：人物群体、地理沿革、物产资源、水利工程、祠祀宗教、灾异荒政、学校科举、风俗礼仪等

## 页面范围：第 {page_start} 页 到 第 {page_end} 页

以下是每页的文本内容：
---
{page_texts}
---

只输出 JSON，不要其他文字。"""

CONSOLIDATION_PROMPT = """你是一个严谨的中国地方志专题整理助手。你已收到多批页面各自发现的专题候选，现在需要整合去重，形成最终专题列表。

## 输入：各批次的专题候选结果

{batch_summaries}

## 输出 JSON 格式（必须严格遵守）
{{
  "专题列表": [
    {{
      "专题名称": "最终确定的专题名称",
      "专题描述": "整合后的专题描述（100-300字），说明该专题在这份方志中的覆盖范围和证据基础",
      "核心词": ["去重整合后的核心词1", "核心词2"],
      "扩展词": ["去重整合后的扩展词1", "扩展词2"],
      "页面池对象": ["整合后的页面池对象1", "页面类型2"],
      "可抽取单元": ["整合后的叙事字段1", "字段2"],
      "可能回答的问题": ["整合后的研究问题1"],
      "证据页码": [页码1, 页码2],
      "佐证摘录": [
        {{"页码": 页码1, "原文": "来自批次候选的原文短句"}},
        {{"页码": 页码2, "原文": "来自批次候选的原文短句"}}
      ]
    }}
  ]
}}

## 整合规则
1. 对同义、近义、上下位、材料来源相近或研究问题相近的专题进行合并，合并成一个更准确、更有解释力的专题名
2. 关键词去重并保留最重要的 5-10 个核心词和 5-10 个扩展词
3. 专题描述要整合各批次的发现，形成完整描述，避免把同一研究方向拆成许多零散小题
4. 优先保留跨批次反复出现、证据链清楚、能形成研究问题的专题；只在确实有独立问题意识时才拆分
5. 删除只有零星词语、目录枚举、泛泛栏目名或缺乏明确证据支撑的微专题
6. 页面池对象、可抽取单元、可能回答的问题要随专题合并同步整合，避免重复、空泛或互相冲突
7. 证据页码和佐证摘录必须从各批次候选结果中继承或合并，不能新增未给出的页码和原文
8. 每个专题保留最有代表性的证据页码和佐证摘录，优先覆盖不同页面和不同材料类型
9. 批次边界不是专题边界；不要因为候选来自不同批次、不同页段、不同县名或不同栏目就机械拆成多个专题
10. 面对大文件时尤其要压缩同质候选，把“同一研究问题的不同表述”合并为一个更稳定的专题；不要输出大量只差一两个关键词的专题

只输出 JSON，不要其他文字。"""

CHAT_SYSTEM_PROMPT_TOPICS = """你是一个数字人文研究助手，专门帮助用户从清代方志古籍资料中发现和确定研究专题。

你的工作方式：
1. 必须先依据当前文件的底表内容样本，了解这批方志实际涵盖了哪些内容
2. 主动向用户建议可能的研究专题，但只能提出底表文本中有证据支撑的专题
3. 与用户讨论确认最终的专题列表
4. 为每个专题确定：专题名称、描述、核心关键词、扩展关键词、页面池对象、可抽取单元、可能回答的问题

关键词设计要求：
- 核心词：直接表示专题的词，要来自或贴近底表文本
- 扩展词：相关但间接的词，要服务于后续在底表中检索页面
- 要同时包含简体和繁体变体

当用户确认专题后，请用以下 JSON 格式输出以方便系统解析：
```json
{
  "专题列表": [
    {
      "专题名称": "专题名",
      "专题描述": "专题描述文本",
      "核心词": ["词1", "词2", "词3"],
      "扩展词": ["词1", "词2", "词3"],
      "页面池对象": ["页面类型或对象1", "对象2"],
      "可抽取单元": ["字段1", "字段2"],
      "可能回答的问题": ["研究问题1"]
    }
  ]
}
```

请用中文回复，保持学术严谨但易懂的风格。"""


# ============================================================
# JSON 解析
# ============================================================

def _parse_topic_json(response: str) -> dict:
    """从 LLM 响应中提取专题 JSON。先尝试直接解析，再尝试正则提取 JSON 块。"""
    try:
        return json.loads(response.strip())
    except json.JSONDecodeError:
        pass

    # 尝试提取 ```json ... ``` 代码块
    match = re.search(r'```(?:json)?\s*([\s\S]*?)```', response)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 尝试提取第一个完整 JSON 对象
    match = re.search(r'\{[\s\S]*"专题列表"[\s\S]*\}', response)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    raise ValueError(f"无法从回复中解析专题 JSON: {response[:300]}")


# ============================================================
# 核心流程
# ============================================================

async def _extract_one_batch(
    batch_no: int,
    page_start: int,
    page_end: int,
    pages: list[PageContent],
    llm_config: dict | None,
    sem: asyncio.Semaphore,
) -> dict:
    """单个批次的专题提取，返回 {batch_no, page_range, topics, response, error}"""
    # 构建页面文本
    page_lines = []
    for page in pages:
        text = (page.content or "").strip()
        if not text:
            continue
        sample = text[:MAX_CHARS_PER_PAGE]
        page_lines.append(f"第{page.page_no}页：{sample}")

    if not page_lines:
        return {
            "batch_no": batch_no,
            "page_range": f"{page_start}-{page_end}",
            "topics": [],
            "response": "",
            "error": None,
        }

    prompt = BATCH_EXTRACTION_PROMPT.format(
        page_start=page_start,
        page_end=page_end,
        page_texts="\n\n".join(page_lines),
    )

    try:
        async with sem:
            response = await llm_service.chat([
                {"role": "system", "content": "你是一个严谨的中国地方志专题发现助手。请只输出 JSON，不要其他文字。"},
                {"role": "user", "content": prompt},
            ], runtime_config=llm_config)
    except Exception as e:
        logger.error(f"批次 {batch_no} (第{page_start}-{page_end}页) LLM 调用失败: {e}")
        return {
            "batch_no": batch_no,
            "page_range": f"{page_start}-{page_end}",
            "topics": [],
            "response": "",
            "error": str(e),
        }

    try:
        parsed = _parse_topic_json(response)
        topics = parsed.get("专题列表", [])
    except Exception as e:
        logger.warning(f"批次 {batch_no} JSON 解析失败: {e}")
        topics = []

    return {
        "batch_no": batch_no,
        "page_range": f"{page_start}-{page_end}",
        "topics": topics,
        "response": response,
        "error": None,
    }


async def _consolidate_batches(
    batch_results: list[dict],
    llm_config: dict | None,
) -> dict:
    """最终合并：将所有批次结果汇总，让 LLM 去重合并"""
    # 构建批次摘要（只用成功且有结果的批次）
    summaries = []
    for br in batch_results:
        if br["error"] is not None:
            summaries.append(f"## 批次 {br['batch_no']}（第{br['page_range']}页）\n**提取失败**：{br['error']}")
        elif not br["topics"]:
            summaries.append(f"## 批次 {br['batch_no']}（第{br['page_range']}页）\n未发现明显专题")
        else:
            topic_text = json.dumps({"专题列表": br["topics"]}, ensure_ascii=False, indent=2)
            summaries.append(f"## 批次 {br['batch_no']}（第{br['page_range']}页）\n{topic_text}")

    if not summaries:
        return {"专题列表": []}

    prompt = CONSOLIDATION_PROMPT.format(batch_summaries="\n\n".join(summaries))

    try:
        response = await llm_service.chat([
            {"role": "system", "content": "你是一个严谨的中国地方志专题整理助手。请只输出 JSON，不要其他文字。"},
            {"role": "user", "content": prompt},
        ], runtime_config=llm_config)
    except Exception as e:
        logger.error(f"专题合并 LLM 调用失败: {e}")
        # 合并失败时，把各批次的结果直接汇总去重返回
        return _fallback_merge(batch_results)

    try:
        parsed = _parse_topic_json(response)
        topics = _fill_topic_evidence(parsed.get("专题列表", []), batch_results)
        return {"专题列表": topics, "合并原文": response}
    except Exception as e:
        logger.warning(f"合并结果 JSON 解析失败: {e}，使用 fallback 合并")
        return _fallback_merge(batch_results)


def _fill_topic_evidence(topics: list[dict], batch_results: list[dict]) -> list[dict]:
    """最终合并结果缺证据时，从批次候选中按专题名补回页码和摘录。"""
    evidence_by_name: dict[str, dict] = {}
    for br in batch_results:
        for topic in br.get("topics", []):
            name = str(topic.get("专题名称", "")).strip()
            if not name:
                continue
            bucket = evidence_by_name.setdefault(name, {"证据页码": [], "佐证摘录": []})
            for page_no in topic.get("证据页码") or []:
                if page_no not in bucket["证据页码"]:
                    bucket["证据页码"].append(page_no)
            for evidence in topic.get("佐证摘录") or topic.get("证据摘录") or []:
                if not isinstance(evidence, dict):
                    continue
                quote = str(evidence.get("原文") or evidence.get("摘录") or "").strip()
                if not quote:
                    continue
                item = {"页码": evidence.get("页码"), "原文": quote[:220]}
                if item not in bucket["佐证摘录"]:
                    bucket["佐证摘录"].append(item)

    filled: list[dict] = []
    for topic in topics:
        if not isinstance(topic, dict):
            continue
        name = str(topic.get("专题名称", "")).strip()
        source = evidence_by_name.get(name, {})
        if not topic.get("证据页码") and source.get("证据页码"):
            topic["证据页码"] = source["证据页码"][:8]
        if not topic.get("佐证摘录") and source.get("佐证摘录"):
            topic["佐证摘录"] = source["佐证摘录"][:5]
        filled.append(topic)
    return filled


def _fallback_merge(batch_results: list[dict]) -> dict:
    """当合并 LLM 调用失败时，简单去重合并各批次结果"""
    seen = set()
    merged = []
    for br in batch_results:
        for topic in br.get("topics", []):
            name = topic.get("专题名称", "")
            if name and name not in seen:
                seen.add(name)
                merged.append(topic)
    return {"专题列表": _fill_topic_evidence(merged, batch_results), "合并方式": "简单去重（合并LLM调用失败）"}


async def run_topic_extraction(
    task_id,
    proj_id: str,
    document_id: str,
    llm_config: dict | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    llm_concurrency: int = DEFAULT_CONCURRENCY,
    **kwargs,
) -> dict:
    """批量专题提取后台任务"""
    from uuid import UUID

    t_id = UUID(str(task_id))
    doc_id = UUID(str(document_id))


    # 1. 加载所有 OCR 完成的页面
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

    # 2. 分批
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

    batch_count = len(batches)
    await task_manager.update_progress(t_id, 5, {"current": 0, "total": total, "type": "topic_extract"})
    logger.info(f"专题提取：共 {total} 页，分为 {batch_count} 个批次（每批 {batch_size} 页）")

    # 3. 并发提取各批次
    sem = asyncio.Semaphore(max(1, int(llm_concurrency or DEFAULT_CONCURRENCY)))

    async def _process_batch(b: dict) -> dict:
        await task_manager.update_progress(
            t_id,
            5,
            {
                "current": max(0, b["pages"][0].page_no - 1),
                "total": total,
                "type": "topic_extract",
                "message": f"模型正在分析第{b['page_start']}-{b['page_end']}页",
            },
        )
        return await _extract_one_batch(
            batch_no=b["batch_no"],
            page_start=b["page_start"],
            page_end=b["page_end"],
            pages=b["pages"],
            llm_config=llm_config,
            sem=sem,
        )

    futures = [asyncio.create_task(_process_batch(b)) for b in batches]
    batch_results = []
    completed = 0
    try:
        for coro in asyncio.as_completed(futures):
            br = await coro
            batch_results.append(br)
            completed += 1
            progress = int(completed / batch_count * 80) + 5
            await task_manager.update_progress(
                t_id,
                progress,
                {
                    "current": min(completed * batch_size, total),
                    "total": total,
                    "type": "topic_extract",
                    "message": f"已完成 {completed}/{batch_count} 个专题提取批次",
                },
            )
    except asyncio.CancelledError:
        for task in futures:
            task.cancel()
        await asyncio.gather(*futures, return_exceptions=True)
        raise

    # 按批次号排序
    batch_results.sort(key=lambda x: x["batch_no"])
    all_batches_failed = batch_results and all(br.get("error") for br in batch_results)
    if all_batches_failed:
        reasons = "；".join(str(br.get("error") or "未知错误") for br in batch_results[:3])
        raise RuntimeError(f"专题提取批次全部失败：{reasons}")

    await task_manager.update_progress(
        t_id,
        85,
        {
            "current": total,
            "total": total,
            "type": "topic_extract",
            "message": "模型正在合并专题结果",
        },
    )

    # 4. 最终合并
    consolidated = await _consolidate_batches(batch_results, llm_config)
    await task_manager.update_progress(
        t_id,
        95,
        {
            "current": total,
            "total": total,
            "type": "topic_extract",
            "message": "专题结果整理完成",
        },
    )

    return {
        "文件ID": str(doc_id),
        "总页数": total,
        "批次数量": batch_count,
        "批次大小": batch_size,
        "批次结果": [
            {
                "批次号": br["batch_no"],
                "页码范围": br["page_range"],
                "专题数": len(br["topics"]),
                "状态": "失败" if br["error"] else "成功",
                "LLM原文": br["response"],
            }
            for br in batch_results
        ],
        "最终专题列表": consolidated.get("专题列表", []),
        "合并原文": consolidated.get("合并原文", consolidated.get("合并方式", "")),
    }
