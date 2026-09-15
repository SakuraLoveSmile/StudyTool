import sys
import os
import json
import threading

# 1. Immediate dispatch for offline utility commands (T3: backup/restore offline mode)
if len(sys.argv) > 1 and sys.argv[1] in {"backup", "restore"}:
    from app.backup import run_cli
    run_cli()
    sys.exit(0)

_desktop_bootstrapped = False

def bootstrap_desktop(input_stream=None):
    """Centralized desktop bootstrap: reads configuration payload once from stdin,
    starts parent-exit watchdog, and applies desktop settings to app config."""
    global _desktop_bootstrapped
    if _desktop_bootstrapped:
        return
    _desktop_bootstrapped = True
    os.environ["RAG_DESKTOP_MODE"] = "1"

    stream = input_stream or sys.stdin
    line = stream.readline()
    if not line:
        sys.stderr.write("Fatal: Missing desktop bootstrap payload on standard input\n")
        sys.exit(1)

    try:
        bootstrap_data = json.loads(line)
    except Exception as exc:
        sys.stderr.write(f"Fatal: Malformed desktop bootstrap payload: {exc}\n")
        sys.exit(1)

    # Start watchdog thread to monitor stdin EOF (closes when Electron exits)
    def _watchdog_stdin():
        try:
            while True:
                b = stream.read(1)
                if not b:
                    # Pipe was closed by parent process
                    os._exit(0)
        except Exception:
            os._exit(0)

    watchdog = threading.Thread(target=_watchdog_stdin, daemon=True)
    watchdog.start()

    from app.config import settings
    settings.apply_desktop_payload(bootstrap_data)

from app.config import settings
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.database import init_db
from app.services.task_worker import TaskWorker
from app.services.http_client import HttpClientProvider
from app.routers import materials, questions, analysis, cards, practice, learning
from app.routers import auth
from app.auth import init_auth_db, enabled_user_ids
from app.access import AccessMiddleware
from app.desktop import desktop_router, DesktopAccessMiddleware, setup_desktop_static_serving
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

# CORS middleware for local Vite frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
        "http://localhost:5173",
        "http://localhost:5174",
        "http://127.0.0.1:18764",
        "http://localhost:18764",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(AccessMiddleware)
app.add_middleware(DesktopAccessMiddleware)

# Register Routers
app.include_router(materials.router)
app.include_router(questions.router)
app.include_router(analysis.router)
app.include_router(cards.router)
app.include_router(practice.router)
app.include_router(learning.router)
app.include_router(auth.router)
app.include_router(desktop_router)

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

# Mount static frontend and SPA fallback when dist is present
setup_desktop_static_serving(app)

if __name__ == "__main__":
    import uvicorn
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--desktop", action="store_true", help="Run in desktop mode")
    parser.add_argument("--port", type=int, default=None, help="Port to listen on")
    parser.add_argument("--host", type=str, default=None, help="Host to listen on")
    args, _ = parser.parse_known_args()

    if args.desktop or os.environ.get("RAG_DESKTOP_MODE") == "1":
        bootstrap_desktop()

    from app.config import settings
    host = args.host or settings.HOST
    port = args.port or settings.PORT

    # Pass app instance directly in desktop/public mode to avoid re-importing and re-reading stdin
    if settings.DESKTOP_MODE or settings.PUBLIC_MODE:
        uvicorn.run(app, host=host, port=port, reload=False)
    else:
        uvicorn.run("app.main:app", host=host, port=port, reload=True)
