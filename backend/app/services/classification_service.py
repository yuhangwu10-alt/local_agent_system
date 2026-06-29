import asyncio
import json
import logging
import re

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.page import PageContent
from app.models.project import SourceDocument
from app.services import llm_service
from app.services.task_manager import task_manager

logger = logging.getLogger(__name__)

# 分类默认并发数（前端可覆盖）
DEFAULT_CONCURRENT = 5

# 页级分类 prompt
CLASSIFICATION_PROMPT = """你是一个严谨的中国地方志页级文本结构化助手。

请对以下古籍页面文本进行分类，输出 JSON 格式。

## 材料本体分类（23 类，选 1 个主分类）
出版说明_版权信息、目录凡例序跋、建置沿革、山川地理、物产资源、水利交通、
户口田赋、市镇商贸、盐业矿产、学校选举、职官政绩、人物传记、人物伦理传记、
风俗礼仪、宗教祠祀、兵防城池、灾异荒政、艺文碑刻、地名族群、
图题图版页、编修名录页、非正文污染页、混合难判页

## 输出 JSON 格式（必须严格遵守）
{{
  "材料分类": "主分类名称",
  "次分类": "次分类1；次分类2",
  "研究映射": ["研究标签1", "研究标签2"],
  "是否正文": true或false,
  "材料标记": {{"分类名": 1或0, ...}},
  "研究标记": {{"研究标签": 1或0, ...}},
  "要素标记": {{"要素名": 1或0, ...}},
  "信号标记": {{"信号名": 1或0, ...}},
  "页面摘要": "一句话摘要",
  "证据句1": "关键证据句",
  "证据句2": "第二证据句",
  "置信度": 0.0到1.0
}}

## 判断规则
- 是否正文：非出版说明、非目录序跋、非污染页、文字>=30字
- 材料标记：判断该页是否属于每个材料类别（1=是，0=否）
- 研究标记：判断该页可映射到哪些研究主题（1=是，0=否）
- 要素标记：页面文本中是否包含该地理/社会要素
- 信号标记：页面文本中是否出现特定叙事关键词信号
- 只根据输入文本判断，不可臆造

页面文本：
---
{content}
---

只输出 JSON，不要其他文字。"""


def _compute_quality_grade(classification: dict, text_len: int) -> str:
    """计算质量等级 A/B/C/D"""
    status = classification.get("处理状态", "成功")
    is_main = classification.get("是否正文", False)
    confidence = classification.get("置信度", 0.0)

    if status == "失败":
        return "D"
    if text_len < 50:
        return "D"
    if is_main and text_len >= 200 and confidence >= 0.7:
        return "A"
    if is_main and text_len >= 200:
        return "B"
    if is_main and text_len >= 50:
        return "B"
    if not is_main:
        return "C"
    return "C"


def _compute_text_density(text_len: int, has_summary: bool, has_evidence: bool) -> int:
    """计算文本密度分 (0-100)"""
    score = 0
    if text_len >= 2000:
        score += 60
    elif text_len >= 1000:
        score += 50
    elif text_len >= 500:
        score += 40
    elif text_len >= 200:
        score += 30
    elif text_len >= 100:
        score += 20
    elif text_len >= 50:
        score += 10
    else:
        score += 2
    if has_summary:
        score += 20
    if has_evidence:
        score += 20
    return score


def _compute_research_signal(is_main: bool, classification: dict) -> int:
    """计算研究信号分 (0-100)"""
    score = 0
    if is_main:
        score += 25
    material = classification.get("材料分类", "")
    if material and material not in ("非正文污染页", "图题图版页", "编修名录页"):
        score += 15
    research = classification.get("研究映射", [])
    if research:
        score += min(15, len(research) * 5)
    evidence1 = classification.get("证据句1", "")
    if evidence1:
        score += 15
    return score


def _determine_recommended_usage(grade: str, density: int, signal: int) -> str:
    """确定推荐用途"""
    if grade == "A" and signal >= 60:
        return "可用于案例分析"
    if grade == "A" and density >= 50:
        return "可用于文本分析"
    if grade in ("A", "B") and signal >= 30:
        return "可用于统计分析"
    if grade == "B":
        return "仅作补充材料"
    return "暂不建议使用"


def _parse_classification(response: str) -> dict:
    """从 LLM 响应中提取分类结果"""
    try:
        return json.loads(response.strip())
    except json.JSONDecodeError:
        pass
    match = re.search(r'\{.*\}', response, re.DOTALL)
    if match:
        return json.loads(match.group())
    raise ValueError(f"无法解析分类结果: {response[:200]}")


async def run_classification(task_id, proj_id, document_id: str | None = None, llm_config: dict | None = None, llm_concurrency: int = DEFAULT_CONCURRENT, **kwargs):
    """页级分类后台任务"""
    from uuid import UUID

    t_id = UUID(str(task_id))
    p_id = UUID(str(proj_id))
    doc_id = UUID(str(document_id)) if document_id else None


    # 获取项目下所有已 OCR 的页面
    async with async_session() as db:
        conditions = [
            SourceDocument.project_id == p_id,
            PageContent.ocr_status == "completed",
            PageContent.content.isnot(None),
            PageContent.content != "",
        ]
        if doc_id:
            conditions.append(PageContent.document_id == doc_id)

        result = await db.execute(
            select(PageContent)
            .join(SourceDocument, PageContent.document_id == SourceDocument.id)
            .where(*conditions)
            .order_by(PageContent.page_no)
        )
        pages = result.scalars().all()

    # BE-FIX-009: 无 OCR 页面时抛异常
    if not pages:
        raise ValueError("没有已 OCR 的页面，请先完成 OCR 处理")

    total = len(pages)
    await task_manager.update_progress(t_id, 5, {"current": 0, "total": total, "type": "classify"})
    # BE-FIX-004: 先在内存中收集结果，通过失败率检查后再写入数据库
    results: list[tuple[PageContent, dict]] = []
    failed_count = 0

    sem = asyncio.Semaphore(max(1, int(llm_concurrency or DEFAULT_CONCURRENT)))

    async def _classify_one(page: PageContent) -> tuple[PageContent, dict, bool]:
        """分类单页，返回 (page, classification, is_error)"""
        content = (page.content or "")[:3000]
        try:
            async with sem:
                response = await llm_service.chat([
                    {"role": "system", "content": "你是一个严谨的中国地方志页级文本结构化助手。"},
                    {"role": "user", "content": CLASSIFICATION_PROMPT.format(content=content)},
                ], runtime_config=llm_config)
            classification = _parse_classification(response)

            # 补充计算字段
            text_len = len(page.content or "")
            has_summary = bool(classification.get("页面摘要"))
            has_evidence = bool(classification.get("证据句1"))
            is_main = classification.get("是否正文", False)

            classification["质量等级"] = _compute_quality_grade(classification, text_len)
            classification["等级原因"] = f"正文页" if is_main else "非正文页"
            classification["文本密度分"] = _compute_text_density(text_len, has_summary, has_evidence)
            classification["研究信号分"] = _compute_research_signal(is_main, classification)
            classification["推荐用途"] = _determine_recommended_usage(
                classification["质量等级"],
                classification["文本密度分"],
                classification["研究信号分"],
            )
            return (page, classification, False)

        except Exception as e:
            logger.error(f"分类失败 第{page.page_no}页: {e}")
            classification = {
                "材料分类": "混合难判页",
                "质量等级": "D",
                "等级原因": f"分类失败: {e}",
                "是否正文": False,
            }
            return (page, classification, True)

    # 并发执行，as_completed 实时更新进度
    futures = [asyncio.create_task(_classify_one(page)) for page in pages]
    completed = 0
    try:
        for coro in asyncio.as_completed(futures):
            page, classification, is_error = await coro
            results.append((page, classification))
            if is_error:
                failed_count += 1
            completed += 1
            progress = int(completed / total * 90) + 5
            await task_manager.update_progress(t_id, progress, {"current": completed, "total": total, "type": "classify"})
    except asyncio.CancelledError:
        for task in futures:
            task.cancel()
        await asyncio.gather(*futures, return_exceptions=True)
        raise

    # BE-FIX-004: 检查失败率，通过后再写入数据库
    fail_ratio = failed_count / total if total > 0 else 0
    if fail_ratio > 0.5:
        raise RuntimeError(f"分类失败率过高: {fail_ratio:.0%}（{failed_count}/{total}），不写入数据库")

    # 批量写入数据库
    batch: list[PageContent] = []
    for page, classification in results:
        page.classification = classification
        batch.append(page)
        if len(batch) >= 20:
            async with async_session() as db:
                db.add_all(batch)
                await db.commit()
            batch = []

    if batch:
        async with async_session() as db:
            db.add_all(batch)
            await db.commit()

    return {"项目ID": str(p_id), "分类页数": total, "失败页数": failed_count}
