import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.database import init_db
from app.services.task_worker import TaskWorker
from app.services.http_client import HttpClientProvider
from app.routers import materials, questions, analysis, cards, practice, learning
from app.routers import auth
from app.auth import init_auth_db, enabled_user_ids
from app.access import AccessMiddleware
from app.tenancy import tenant_scope
from app.services.request_operations import stop_operations

from app.diagnostics import setup_diagnostics, shutdown_diagnostics

setup_diagnostics()
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    setup_diagnostics()
    logger.info("Initializing database...")
    if settings.PUBLIC_MODE:
        from urllib.parse import urlsplit
        origin = urlsplit(settings.PUBLIC_ORIGIN)
        if origin.scheme != "https" or not origin.netloc or origin.path not in {"", "/"} or origin.query or origin.fragment:
            raise RuntimeError("PUBLIC_ORIGIN must be a single HTTPS origin")
        if not settings.AUTH_COOKIE_SECURE:
            raise RuntimeError("Public mode requires secure cookies")
        if settings.DEMO_MODE or settings.OCR_MODE != "vision_llm" or settings.EMBEDDING_MODE != "remote":
            raise RuntimeError("Public mode requires real LLM, vision OCR and remote embeddings")
        init_auth_db()
        for user_id in enabled_user_ids():
            with tenant_scope(user_id):
                init_db()
    else:
        init_db()
    await HttpClientProvider.start()
    logger.info("Starting background TaskWorker...")
    if settings.PUBLIC_MODE:
        TaskWorker.start_all()
    else:
        TaskWorker.start()
    try:
        yield
    finally:
        logger.info("Stopping background TaskWorker...")
        if settings.PUBLIC_MODE:
            await TaskWorker.stop_all()
        else:
            TaskWorker.stop()
        await stop_operations()
        await HttpClientProvider.close()
        shutdown_diagnostics()

app = FastAPI(
    title="专业课备考助手 MVP",
    description="单机个人专业课备考知识库、考点统计、背诵卡与练习批改系统",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None if settings.PUBLIC_MODE else "/docs",
    redoc_url=None if settings.PUBLIC_MODE else "/redoc",
    openapi_url=None if settings.PUBLIC_MODE else "/openapi.json",
)

# CORS middleware for the local Vite frontend.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://127.0.0.1:5174", "http://localhost:5173", "http://localhost:5174"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(AccessMiddleware)

# Register Routers
app.include_router(materials.router)
app.include_router(questions.router)
app.include_router(analysis.router)
app.include_router(cards.router)
app.include_router(practice.router)
app.include_router(learning.router)
app.include_router(auth.router)

@app.get("/health")
def health():
    return {"status": "ok", "service": "exam-prep-assistant-backend"}

@app.get("/api/config/status")
def get_config_status():
    return {
        "host": settings.HOST,
        "port": settings.PORT,
        "data_dir": "private" if settings.PUBLIC_MODE else str(settings.data_path),
        "llm_model": settings.LLM_MODEL,
        "llm_base_url": settings.LLM_BASE_URL,
        "llm_key_configured": bool(settings.LLM_API_KEY),
        "demo_mode": settings.DEMO_MODE,
        "ocr_mode": settings.OCR_MODE,
        "embedding_mode": settings.EMBEDDING_MODE
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=settings.HOST, port=settings.PORT, reload=not settings.PUBLIC_MODE)
