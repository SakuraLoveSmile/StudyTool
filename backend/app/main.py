import sys
import os

# Make `app.*` importable no matter how this file was launched.
#   * `python -m app.main` (cwd = backend) already resolves it;
#   * `python <repo>/backend/app/main.py` — which is exactly how
#     desktop/src/backend-manager.ts spawns the DEVELOPMENT backend — puts `backend/app`
#     on sys.path instead, so every `from app...` import would raise ModuleNotFoundError.
# PyInstaller builds are unaffected: the bootloader already exposes the bundle root.
# Frozen builds already expose the bundle root; touching sys.path there would only risk
# interfering with PyInstaller's own importer.
if not getattr(sys, "frozen", False):
    _BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _BACKEND_ROOT not in sys.path:
        sys.path.insert(0, _BACKEND_ROOT)

import json
import logging
import threading

# 1. Immediate dispatch for offline utility commands (T3: backup/restore offline mode)
if len(sys.argv) > 1 and sys.argv[1] in {"backup", "restore"}:
    from app.backup import run_cli
    run_cli()
    sys.exit(0)

_desktop_bootstrapped = False

_stdin_watchdog_stream = None
_stdin_watchdog_started = False


def start_parent_exit_watchdog() -> None:
    """Starts the stdin-EOF watchdog that exits when the Electron parent goes away.

    Started AFTER the business modules are imported, not from bootstrap_desktop(): the
    watchdog is only needed to notice the parent dying, and keeping the import phase
    free of an extra thread blocked on stdin avoids a second live thread while the
    frozen Windows build loads its native extensions.
    """
    global _stdin_watchdog_started
    if _stdin_watchdog_started or _stdin_watchdog_stream is None:
        return
    _stdin_watchdog_started = True

    def _watchdog_stdin():
        try:
            while True:
                if not _stdin_watchdog_stream.read(1):
                    # Pipe was closed by parent process
                    os._exit(0)
        except Exception:
            os._exit(0)

    threading.Thread(target=_watchdog_stdin, daemon=True).start()


def bootstrap_desktop(input_stream=None):
    """Centralized desktop bootstrap: reads the configuration payload once from stdin
    and applies it to app config, before any business module is imported."""
    global _desktop_bootstrapped, _stdin_watchdog_stream
    if _desktop_bootstrapped:
        return
    _desktop_bootstrapped = True
    os.environ["STUDYTOOL_DESKTOP_MODE"] = "1"

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

    # Remembered here; started later by start_parent_exit_watchdog().
    _stdin_watchdog_stream = stream

    from app.config import settings
    settings.apply_desktop_payload(bootstrap_data)

from app.config import settings
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# --- Startup ordering guarantee (T1) -------------------------------------------------
# Desktop configuration and the diagnostics subsystem MUST be initialized before any
# business module below is imported: an import-time failure (missing module, broken
# dependency) then still lands in backend.jsonl instead of dying on stderr only.
# `bootstrap_desktop()` stays gated on script execution so importing this module (tests,
# uvicorn --reload child, tooling) can never block on reading stdin.
_running_as_script = __name__ == "__main__"
if _running_as_script and ("--desktop" in sys.argv or os.environ.get("STUDYTOOL_DESKTOP_MODE") == "1"):
    bootstrap_desktop()

from app.diagnostics import (
    setup_diagnostics,
    shutdown_diagnostics,
    log_diagnostic,
    extract_safe_exception_info,
    sanitize_endpoint,
)

setup_diagnostics()
log_diagnostic(
    "startup.diagnostics.ready",
    message="桌面配置与日志系统已初始化（先于业务模块导入）",
    phase="bootstrap",
)

# Startup hang watchdog: if the process is still starting after 60s, dump every thread's
# stack to stderr once. A frozen build that blocks during import produces no traceback and
# no further log records, leaving "the backend never became ready" as the only symptom;
# the dump names the exact line it is stuck on. Disarmed as soon as startup completes.
_startup_dump_armed = False
if _running_as_script:
    try:
        import faulthandler

        faulthandler.dump_traceback_later(20, repeat=True, exit=False)
        _startup_dump_armed = True
    except Exception:  # pragma: no cover - diagnostics must never block startup
        _startup_dump_armed = False

def _importing(module_name: str) -> None:
    """Records which business module import is in progress.

    A frozen build that never becomes ready leaves no traceback, so "it stopped at
    module X" is the difference between a one-line diagnosis and a guessing game. The
    call is placed BEFORE each import: the last recorded target is the import that
    hung.
    """
    log_diagnostic(
        "startup.import.step",
        message=f"正在导入业务模块：{module_name}",
        phase="import",
        target=module_name,
    )


_importing("app.database")
try:
    from app.database import init_db
    _importing("app.services.task_worker")
    from app.services.task_worker import TaskWorker
    _importing("app.services.http_client")
    from app.services.http_client import HttpClientProvider
    _importing("app.routers")
    from app.routers import materials, questions, analysis, cards, practice, learning
    from app.routers import auth, library
    _importing("app.auth")
    from app.auth import init_auth_db, enabled_user_ids
    _importing("app.access")
    from app.access import AccessMiddleware
    _importing("app.desktop")
    from app.desktop import desktop_router, DesktopAccessMiddleware, setup_desktop_static_serving
    _importing("app.tenancy")
    from app.tenancy import tenant_scope
    _importing("app.services.request_operations")
    from app.services.request_operations import stop_operations
except BaseException as _import_exc:  # noqa: BLE001 - must be recorded before propagating
    log_diagnostic(
        "startup.import_failed",
        level=logging.ERROR,
        message="业务模块导入失败，后台无法启动",
        phase="import",
        error_type=type(_import_exc).__name__,
        exception_info=extract_safe_exception_info(_import_exc, "import"),
    )
    raise

log_diagnostic("startup.imports.ok", message="业务模块导入完成", phase="import")
start_parent_exit_watchdog()
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    setup_diagnostics()
    logger.info("Initializing database...")
    log_diagnostic("startup.lifespan.begin", message="后台应用启动流程开始", phase="lifespan")
    failed_stage = "configuration"
    try:
        if settings.PUBLIC_MODE:
            from urllib.parse import urlsplit
            origin = urlsplit(settings.PUBLIC_ORIGIN)
            if origin.scheme != "https" or not origin.netloc or origin.path not in {"", "/"} or origin.query or origin.fragment:
                raise RuntimeError("PUBLIC_ORIGIN must be a single HTTPS origin")
            if not settings.AUTH_COOKIE_SECURE:
                raise RuntimeError("Public mode requires secure cookies")
            if settings.DEMO_MODE:
                raise RuntimeError("Public mode requires DEMO_MODE=false")
            if settings.OCR_MODE not in {"vision_llm", "paddleocr_api"}:
                raise RuntimeError("Public mode requires OCR_MODE=vision_llm or paddleocr_api")
            if settings.EMBEDDING_MODE != "remote":
                raise RuntimeError("Public mode requires EMBEDDING_MODE=remote")

        failed_stage = "database"
        log_diagnostic("startup.database.begin", message="开始初始化数据库", phase="database")
        if settings.PUBLIC_MODE:
            init_auth_db()
            for user_id in enabled_user_ids():
                with tenant_scope(user_id):
                    init_db()
        else:
            init_db()
        # init_db commits and closes its transaction before returning.  Keep this
        # event after the call so it cannot be mistaken for a pre-commit marker.
        log_diagnostic("startup.database.ready", message="数据库初始化完成", phase="database")

        failed_stage = "http_client"
        log_diagnostic("startup.http_client.begin", message="开始初始化 HTTP client", phase="http_client")
        await HttpClientProvider.start()
        log_diagnostic("startup.http_client.ready", message="HTTP client 初始化完成", phase="http_client")

        failed_stage = "task_worker"
        log_diagnostic("startup.task_worker.begin", message="开始启动后台任务工作器", phase="task_worker")
        logger.info("Starting background TaskWorker...")
        if settings.PUBLIC_MODE:
            TaskWorker.start_all()
        else:
            TaskWorker.start()
        log_diagnostic("startup.task_worker.ready", message="后台任务工作器启动完成", phase="task_worker")
    except BaseException as startup_exc:  # noqa: BLE001 - preserve the original failure
        log_diagnostic(
            "startup.failed",
            level=logging.ERROR,
            message="后台服务启动失败",
            phase="startup",
            failed_stage=failed_stage,
            error_type=type(startup_exc).__name__,
            exception_info=extract_safe_exception_info(startup_exc, failed_stage),
        )
        raise
    log_diagnostic(
        "startup.ready",
        message="后台服务已就绪",
        phase="ready",
        service="backend",
        model=settings.LLM_MODEL,
        endpoint=sanitize_endpoint(settings.LLM_BASE_URL),
    )
    if _startup_dump_armed:
        try:
            import faulthandler

            faulthandler.cancel_dump_traceback_later()
        except Exception:  # pragma: no cover
            pass
    try:
        yield
    finally:
        log_diagnostic("shutdown.begin", message="后台服务开始停机", phase="shutdown")
        logger.info("Stopping background TaskWorker...")
        if settings.PUBLIC_MODE:
            await TaskWorker.stop_all()
        else:
            TaskWorker.stop()
        await stop_operations()
        await HttpClientProvider.close()
        log_diagnostic("shutdown.ready", message="后台服务已停机", phase="shutdown")
        shutdown_diagnostics()

app = FastAPI(
    title="学习助手",
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
app.include_router(library.router)
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

    if args.desktop or os.environ.get("STUDYTOOL_DESKTOP_MODE") == "1":
        bootstrap_desktop()

    from app.config import settings
    host = args.host or settings.HOST
    port = args.port or settings.PORT

    log_diagnostic(
        "startup.uvicorn.begin",
        message="开始监听本地端口",
        phase="uvicorn",
        endpoint=f"http://{host}:{port}",
        service="uvicorn",
    )

    # Pass app instance directly in desktop/public mode to avoid re-importing and re-reading stdin
    if settings.DESKTOP_MODE or settings.PUBLIC_MODE:
        uvicorn.run(app, host=host, port=port, reload=False)
    else:
        uvicorn.run("app.main:app", host=host, port=port, reload=True)
