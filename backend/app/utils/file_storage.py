import re
import uuid
from pathlib import Path

from app.config import settings


def get_storage_path() -> Path:
    return Path(settings.storage_path)


def get_input_dir(project_id: uuid.UUID) -> Path:
    return get_storage_path() / "input" / str(project_id)


def get_export_dir(theme_id: uuid.UUID) -> Path:
    return get_storage_path() / "exports" / str(theme_id)


def get_page_images_dir(document_id: uuid.UUID) -> Path:
    return get_storage_path() / "page_images" / str(document_id)


def ensure_dir(path: Path) -> Path:
    """确保目录存在"""
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_join(base_dir: Path, relative_path: str) -> Path:
    """安全地拼接路径，防止路径遍历攻击"""
    # 清理路径中的 .. 和多余分隔符
    cleaned = Path(relative_path)
    # 拒绝绝对路径
    if cleaned.is_absolute():
        raise ValueError(f"拒绝绝对路径: {relative_path}")
    # 拒绝包含 .. 的路径
    if ".." in cleaned.parts:
        raise ValueError(f"拒绝路径遍历: {relative_path}")
    resolved = (base_dir / cleaned).resolve()
    base_resolved = base_dir.resolve()
    if not resolved.is_relative_to(base_resolved):
        raise ValueError(f"路径逃逸出基础目录: {relative_path}")
    return resolved


def sanitize_filename(name: str) -> str:
    """清理文件名，移除危险字符"""
    # 只保留字母、数字、中文、连字符、下划线、点
    cleaned = re.sub(r'[^\w一-鿿\-\.]', '_', name)
    # 移除连续的点（防止 ..）
    cleaned = re.sub(r'\.{2,}', '.', cleaned)
    # 限制长度
    if len(cleaned) > 200:
        name_part, _, ext = cleaned.rpartition('.')
        cleaned = name_part[:190] + '.' + ext if ext else name_part[:200]
    return cleaned or "unnamed"
