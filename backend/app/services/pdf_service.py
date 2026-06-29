import asyncio
import logging
from pathlib import Path

import fitz  # PyMuPDF

from app.config import settings

logger = logging.getLogger(__name__)


async def pdf_to_images(
    pdf_path: Path,
    start_page: int | None = None,
    end_page: int | None = None,
) -> list[tuple[int, bytes]]:
    """将 PDF 转换为 JPEG 图片列表。页码从 1 开始。用于小 PDF。"""

    def _convert():
        with fitz.open(str(pdf_path)) as doc:
            total = doc.page_count
            s = (start_page - 1) if start_page else 0
            e = end_page if end_page else total

            images = []
            mat = fitz.Matrix(settings.ocr_pdf_render_scale, settings.ocr_pdf_render_scale)
            for i in range(s, min(e, total)):
                page = doc[i]
                pix = page.get_pixmap(matrix=mat)
                img_bytes = pix.tobytes("jpeg")
                images.append((i + 1, img_bytes))
            return images

    return await asyncio.to_thread(_convert)


async def get_pdf_page_count(pdf_path: Path) -> int:
    """获取 PDF 页数"""

    def _count():
        with fitz.open(str(pdf_path)) as doc:
            return doc.page_count

    return await asyncio.to_thread(_count)


async def get_page_image(pdf_path: Path, page_no: int) -> bytes:
    """获取单页图片（1-based 页码），用于逐页 OCR 避免内存问题"""

    def _get():
        with fitz.open(str(pdf_path)) as doc:
            page = doc[page_no - 1]
            mat = fitz.Matrix(settings.ocr_pdf_render_scale, settings.ocr_pdf_render_scale)
            pix = page.get_pixmap(matrix=mat)
            return pix.tobytes("jpeg")

    return await asyncio.to_thread(_get)
