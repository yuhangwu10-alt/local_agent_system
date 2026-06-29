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
