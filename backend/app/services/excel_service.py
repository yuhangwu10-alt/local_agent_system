import asyncio
import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)


async def read_ocr_excel(file_path: Path) -> list[dict]:
    """读取已 OCR 的 Excel 文件，返回 [{page_no, content}, ...]"""

    def _read():
        df = pd.read_excel(file_path)
        original_columns = list(df.columns)
        normalized_columns = {str(col).strip().lower(): col for col in df.columns}

        page_aliases = ["page_no", "page", "page_number", "页码", "頁碼", "页面", "頁面"]
        content_aliases = ["content", "text", "ocr_text", "页面内容", "頁面內容", "文本内容", "文本內容", "ocr内容", "ocr內容"]
        page_col = next((normalized_columns[item] for item in page_aliases if item in normalized_columns), None)
        content_col = next((normalized_columns[item] for item in content_aliases if item in normalized_columns), None)
        if page_col is None or content_col is None:
            raise ValueError(
                "Excel 必须包含页码和页面内容列。支持列名：page_no/content，或 页码/页面内容；"
                f"当前列: {original_columns}"
            )

        pages = []
        for _, row in df.iterrows():
            if pd.isna(row[page_col]):
                continue
            pages.append({
                "page_no": int(row[page_col]),
                "content": str(row[content_col]) if pd.notna(row[content_col]) else "",
            })
        if not pages:
            raise ValueError("Excel 中没有可导入的页面内容")
        return pages

    return await asyncio.to_thread(_read)


import re


async def read_topic_excel(file_path: Path) -> list[dict]:
    """读取专题列表 Excel，返回 AI 提取格式的专题列表。

    支持 4 列（对应手动输入表单）：
    - 专题名称（必填）
    - 专属字段 → 映射为 页面池对象
    - 可抽取单元
    - 可能回答的问题

    也兼容旧格式中的「页面池对象」列名。
    """

    def _read():
        df = pd.read_excel(file_path)
        original_columns = list(df.columns)
        col_map = {str(col).strip(): col for col in df.columns}

        # 专题名称列（必填）
        name_aliases = ["专题名称", "主题名称", "名称", "专题"]
        name_col = next((col_map[a] for a in name_aliases if a in col_map), None)
        if name_col is None:
            raise ValueError(
                "Excel 缺少必填列「专题名称」。"
                f"当前列: {original_columns}"
            )

        # 专属字段 → 页面池对象；也兼容直接写「页面池对象」
        fields_aliases = ["专属字段", "页面池对象", "页面池", "对象"]
        fields_col = next((col_map[a] for a in fields_aliases if a in col_map), None)

        # 可抽取单元
        units_aliases = ["可抽取单元", "抽取单元", "叙事单元字段", "单元"]
        units_col = next((col_map[a] for a in units_aliases if a in col_map), None)

        # 可能回答的问题
        questions_aliases = ["可能回答的问题", "研究问题", "问题"]
        questions_col = next((col_map[a] for a in questions_aliases if a in col_map), None)

        def _split(value) -> list[str]:
            if pd.isna(value):
                return []
            text = str(value).strip()
            if not text:
                return []
            return [item.strip() for item in re.split(r"[、，,；;\n\r]+", text) if item.strip()]

        topics = []
        for _, row in df.iterrows():
            name = str(row[name_col]).strip()
            if pd.isna(row[name_col]) or not name:
                continue

            fields = _split(row[fields_col]) if fields_col else []
            units = _split(row[units_col]) if units_col else []
            questions = _split(row[questions_col]) if questions_col else []

            topic = {
                "专题名称": name,
                "专题描述": "",
                "核心词": [],
                "扩展词": [],
                "页面池对象": fields,
                "可抽取单元": units,
                "可能回答的问题": questions,
                "证据页码": [],
                "佐证摘录": [],
            }
            topics.append(topic)

        if not topics:
            raise ValueError("Excel 中没有可导入的专题数据（所有行的专题名称为空）")
        return topics

    return await asyncio.to_thread(_read)


def export_to_excel(data: list[dict], output_path: Path, sheet_name: str = "Sheet1"):
    """导出数据到 Excel"""
    df = pd.DataFrame(data)
    df.to_excel(output_path, index=False, sheet_name=sheet_name)


def export_to_csv(data: list[dict], output_path: Path):
    """导出数据到 CSV"""
    df = pd.DataFrame(data)
    df.to_csv(output_path, index=False, encoding="utf-8-sig")


def export_to_json(data: list[dict], output_path: Path):
    """导出数据到 JSON"""
    import json
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
