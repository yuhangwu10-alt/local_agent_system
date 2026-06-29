import logging
import hashlib
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import chat, documents, export, narratives, ocr_config, pages, projects, tasks, themes
from app.api.pages_pool import router as pages_pool_router
from app.services.task_manager import task_manager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up...")
    await task_manager.recover_on_startup()
    yield
    logger.info("Shutting down...")


app = FastAPI(
    title="多语言古籍专题页池及叙事单元提取系统",
    description="Local OCR, topic discovery, page-pool extraction, narrative-unit extraction, and export system for multilingual ancient books.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(projects.router)
app.include_router(documents.router)
app.include_router(pages.router)
app.include_router(themes.router)
app.include_router(chat.router)
app.include_router(ocr_config.router)
app.include_router(pages_pool_router)
app.include_router(narratives.router)
app.include_router(tasks.router)
app.include_router(export.router)


def frontend_dir() -> Path:
    here = Path(__file__).resolve()
    candidates = [
        here.parents[2] / "static",
        here.parents[1] / "static",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


@app.get("/")
async def root():
    index_file = frontend_dir() / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return {"name": "多语言古籍专题页池及叙事单元提取系统", "version": "0.1.0", "docs": "/docs"}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/app-info")
async def app_info():
    root_path = Path(__file__).resolve().parents[1]
    app_id_file = root_path / ".app_id"
    try:
        if app_id_file.exists():
            app_id = app_id_file.read_text(encoding="utf-8").strip()
        else:
            app_id = hashlib.sha1(f"{root_path}:{uuid.uuid4()}".encode("utf-8")).hexdigest()[:12]
            app_id_file.write_text(app_id, encoding="utf-8")
    except Exception:
        app_id = hashlib.sha1(str(root_path).encode("utf-8")).hexdigest()[:12]
    return {"app_id": app_id}


if frontend_dir().exists():
    app.mount("/static", StaticFiles(directory=frontend_dir()), name="static")
