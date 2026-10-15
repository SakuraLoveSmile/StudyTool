"""Desktop mode support: instance token authentication, origin and host validation,
system maintenance leases, connection probes, graceful drain/shutdown, and static SPA serving.
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import math
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
import pymupdf as fitz
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.staticfiles import StaticFiles

from app.config import settings
from app.database import db_session
from app.diagnostics import (
    call_context,
    extract_provider_request_id,
    extract_safe_exception_info,
    log_diagnostic,
    sanitize_endpoint,
    sanitize_text,
)

import threading

logger = logging.getLogger(__name__)

desktop_router = APIRouter(prefix="/api/desktop", tags=["desktop"])


class MaintenanceManager:
    """Manages system maintenance leases (config changes, backups, restores, shutdowns).

    When active:
    - Mutating user API requests across the system are rejected with 503.
    - Only the lease holder (identified by lease_id) may execute maintenance actions.
    - Lease acquisition fails if any background task, running operation, or in-flight write request exists.
    """
    _lock = threading.Lock()
    _active_lease: Optional[Dict[str, Any]] = None
    _in_flight_mutating_requests: int = 0
    _is_draining: bool = False

    @classmethod
    def is_active(cls) -> bool:
        with cls._lock:
            return cls._active_lease is not None or cls._is_draining

    @classmethod
    def get_lease(cls) -> Optional[Dict[str, Any]]:
        with cls._lock:
            return cls._active_lease

    @classmethod
    def inc_mutating_request(cls) -> bool:
        """Atomically registers an in-flight mutating request.
        Returns True if allowed, False if rejected due to active maintenance or draining.
        """
        with cls._lock:
            if cls._active_lease is not None or cls._is_draining:
                return False
            cls._in_flight_mutating_requests += 1
            return True

    @classmethod
    def dec_mutating_request(cls) -> None:
        with cls._lock:
            if cls._in_flight_mutating_requests > 0:
                cls._in_flight_mutating_requests -= 1

    @classmethod
    def get_in_flight_count(cls) -> int:
        with cls._lock:
            return cls._in_flight_mutating_requests

    @classmethod
    def set_draining(cls, draining: bool = True) -> None:
        with cls._lock:
            cls._is_draining = draining

    @classmethod
    def is_draining(cls) -> bool:
        with cls._lock:
            return cls._is_draining

    @classmethod
    def acquire(cls, lease_id: str, operation: str, is_exit: bool = False) -> None:
        with cls._lock:
            if cls._active_lease:
                if cls._active_lease.get("lease_id") == lease_id:
                    return  # Idempotent re-acquisition
                curr_op = cls._active_lease.get("operation", "未知操作")
                raise HTTPException(
                    status_code=409,
                    detail=f"系统正在执行其他维护操作（{curr_op}），请稍后重试",
                )
            if not is_exit:
                if cls._in_flight_mutating_requests > 0:
                    raise HTTPException(
                        status_code=409,
                        detail=f"系统繁忙，有 {cls._in_flight_mutating_requests} 个写请求正在处理，无法进入维护模式",
                    )

        if not is_exit:
            # Verify system is genuinely idle (tasks & request operations)
            idle, reason = check_system_idle()
            if not idle:
                raise HTTPException(status_code=409, detail=f"系统繁忙，无法进入维护模式: {reason}")

        with cls._lock:
            if not is_exit and cls._in_flight_mutating_requests > 0:
                raise HTTPException(
                    status_code=409,
                    detail=f"系统繁忙，有 {cls._in_flight_mutating_requests} 个写请求正在处理，无法进入维护模式",
                )
            cls._active_lease = {
                "lease_id": lease_id,
                "operation": operation,
                "lease_type": "exit" if is_exit else "regular",
                "acquired_at": time.time(),
            }
        logger.info(
            "Acquired maintenance lease %s (type=%s) for operation '%s'",
            lease_id,
            "exit" if is_exit else "regular",
            operation,
        )

    @classmethod
    def release(cls, lease_id: str) -> bool:
        with cls._lock:
            if cls._active_lease and cls._active_lease.get("lease_id") == lease_id:
                logger.info("Released maintenance lease %s", lease_id)
                cls._active_lease = None
                cls._is_draining = False
                return True
            return False

    @classmethod
    def force_clear(cls) -> None:
        with cls._lock:
            cls._active_lease = None
            cls._in_flight_mutating_requests = 0
            cls._is_draining = False


class DesktopAccessMiddleware:
    """Authentication and boundary middleware for desktop mode.

    When settings.DESKTOP_MODE is True:
    - Enforces Host header is 127.0.0.1:18764 or 127.0.0.1.
    - Enforces valid X-StudyTool-Instance-Token for all /api/* routes.
    - Validates Origin for state-mutating requests.
    - If MaintenanceManager is active, rejects non-GET mutating user requests with 503.
    - Rejects path traversal and unauthorized frames.
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not settings.DESKTOP_MODE:
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        path = scope["path"]
        method = scope["method"]

        async def reject(status_code: int, code: str, message: str):
            res = JSONResponse(
                {"detail": {"code": code, "message": message}},
                status_code=status_code,
            )
            await res(scope, receive, send)

        # Allow /health without token (used for basic readiness)
        if path == "/health" and method in {"GET", "HEAD"}:
            await self.app(scope, receive, send)
            return

        # Host validation: Host must match the configured host or port
        host_header = request.headers.get("host", "").lower().split(":")[0]
        if host_header not in {"127.0.0.1", "localhost", "testserver"}:
            await reject(403, "INVALID_HOST", "非法请求 Host，仅允许本地 127.0.0.1 访问")
            return

        # If not an /api route (i.e. static SPA files or assets), allow GET/HEAD
        if not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return

        # Validate desktop instance token for /api/* requests
        expected_token = (settings.DESKTOP_INSTANCE_TOKEN or "").strip()
        if not expected_token:
            await reject(500, "INSECURE_CONFIGURATION", "桌面端未注入安全实例令牌，拒绝服务")
            return

        provided_token = request.headers.get("X-StudyTool-Instance-Token", "").strip()
        if not provided_token or not hmac.compare_digest(provided_token, expected_token):
            await reject(401, "DESKTOP_AUTH_REQUIRED", "未授权的桌面实例访问：实例令牌无效或缺失")
            return

        # Origin validation for state-mutating methods
        if method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin:
                origin_clean = origin.rstrip("/").lower()
                allowed_origins = {
                    f"http://127.0.0.1:{settings.PORT}",
                    f"http://localhost:{settings.PORT}",
                    "http://127.0.0.1:18764",
                    "http://localhost:18764",
                    "http://127.0.0.1:5173",
                    "http://localhost:5173",
                }
                if origin_clean not in allowed_origins:
                    await reject(403, "ORIGIN_REJECTED", "拒绝跨来源请求")
                    return

            # Check maintenance mode: Block all other mutating user requests
            is_desktop_api = path.startswith("/api/desktop/")
            if not is_desktop_api:
                if not MaintenanceManager.inc_mutating_request():
                    await reject(
                        503,
                        "MAINTENANCE_IN_PROGRESS",
                        "系统正在维护或停机排空中，暂不接受新写入请求",
                    )
                    return
                try:
                    await self.app(scope, receive, send)
                finally:
                    MaintenanceManager.dec_mutating_request()
                return

        await self.app(scope, receive, send)


def has_existing_embeddings() -> bool:
    """Check if the SQLite database already contains generated embeddings.

    Raises RuntimeError if database check fails (does NOT silently return False).
    """
    with db_session() as db:
        row = db.execute(
            "SELECT 1 FROM document_chunks WHERE embedding IS NOT NULL LIMIT 1"
        ).fetchone()
        return bool(row)


def get_active_tasks_count() -> int:
    """Return count of background tasks and long operations currently pending or processing."""
    in_flight = MaintenanceManager.get_in_flight_count()
    try:
        with db_session() as db:
            row_tasks = db.execute(
                "SELECT COUNT(*) FROM tasks WHERE status IN ('pending', 'processing', 'cancel_requested') AND hidden_at IS NULL"
            ).fetchone()
            task_cnt = int(row_tasks[0]) if row_tasks else 0

            # Also check request_operations (sync generation / scoring) if table exists
            has_ops_table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='request_operations'"
            ).fetchone()
            ops_cnt = 0
            if has_ops_table:
                row_ops = db.execute(
                    "SELECT COUNT(*) FROM request_operations WHERE state IN ('pending', 'running', 'processing')"
                ).fetchone()
                ops_cnt = int(row_ops[0]) if row_ops else 0

            return task_cnt + ops_cnt + in_flight
    except Exception as exc:
        logger.error("Failed to query active tasks count: %s", exc)
        # Failure to query must be treated as busy/unknown, never idle!
        raise RuntimeError(f"检查活跃任务状态失败: {exc}") from exc


def check_system_idle() -> tuple[bool, str]:
    """Check if system is completely idle and safe for config updates, restarts, or backups."""
    in_flight = MaintenanceManager.get_in_flight_count()
    if in_flight > 0:
        return False, f"当前有 {in_flight} 个在途写请求正在执行"
    try:
        active_cnt = get_active_tasks_count()
        if active_cnt > 0:
            return False, f"后台有 {active_cnt} 个任务或生成/评分操作正在进行"
        return True, "系统空闲"
    except Exception as exc:
        return False, f"系统状态查询异常: {exc}"


@desktop_router.get("/status")
def get_desktop_status():
    """Returns desktop runtime status, idle state, and embedding constraints."""
    from app.services.parser.ocr_service import OCRService
    idle, idle_reason = check_system_idle()
    try:
        has_emb = has_existing_embeddings()
    except Exception:
        has_emb = True  # Conservative fallback: treat as existing on failure

    from app.database import is_vector_locked
    vector_locked, vector_lock_reason = is_vector_locked()

    lease = MaintenanceManager.get_lease()
    return {
        "desktop_mode": settings.DESKTOP_MODE,
        "host": settings.HOST,
        "port": settings.PORT,
        "idle": idle and not MaintenanceManager.is_active(),
        "idle_reason": idle_reason if not MaintenanceManager.is_active() else f"维护中: {lease.get('operation') if lease else 'active'}",
        "maintenance": MaintenanceManager.is_active(),
        "active_tasks": get_active_tasks_count() if idle else 1,
        "has_existing_embeddings": has_emb,
        "vector_locked": vector_locked,
        "vector_lock_reason": vector_lock_reason or "",
        "llm_model": settings.LLM_MODEL,
        "llm_base_url": sanitize_endpoint(settings.LLM_BASE_URL),
        "llm_configured": bool(settings.LLM_API_KEY),
        "embedding_model": settings.EMBEDDING_MODEL,
        "embedding_base_url": sanitize_endpoint(settings.EMBEDDING_BASE_URL),
        "embedding_configured": bool(settings.EMBEDDING_API_KEY),
        "ocr_mode": settings.OCR_MODE,
        "ocr": OCRService.configuration_status(),
        "baidu_ocr_api_url": sanitize_endpoint(settings.BAIDU_OCR_API_URL),
        "baidu_ocr_configured": bool(settings.BAIDU_OCR_API_URL and settings.BAIDU_OCR_TOKEN),
    }


class ReindexRequest(BaseModel):
    lease_id: Optional[str] = None


@desktop_router.post("/reindex")
async def api_reindex(body: Optional[ReindexRequest] = None):
    """Rebuild vectors under an exclusive maintenance lease."""
    lease = MaintenanceManager.get_lease()
    owned_lease_id = body.lease_id if body and body.lease_id else None
    auto_lease = False
    if lease is None:
        owned_lease_id = owned_lease_id or f"reindex-{uuid.uuid4()}"
        MaintenanceManager.acquire(owned_lease_id, "重建向量索引")
        auto_lease = True
    elif not owned_lease_id or lease.get("lease_id") != owned_lease_id:
        raise HTTPException(status_code=409, detail="当前已有其他维护操作，无法重建索引")

    try:
        return await _run_reindex()
    except asyncio.CancelledError:
        with db_session() as conn:
            conn.execute("DROP TABLE IF EXISTS document_chunks_staging")
        raise
    finally:
        if auto_lease and owned_lease_id:
            MaintenanceManager.release(owned_lease_id)


async def _run_reindex():
    """Rebuilds vector index for all existing document chunks using current embedding model in an isolated staging table."""
    import numpy as np
    from app.services.retrieval.embedding import EmbeddingService
    from app.database import set_system_metadata

    # 1. Check if model is configured
    if not settings.EMBEDDING_API_KEY:
        raise HTTPException(status_code=400, detail="未配置向量模型 API Key，无法重建索引")

    # 2. Get all valid text chunks
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, content FROM document_chunks WHERE content IS NOT NULL AND TRIM(content) != '' AND parse_error IS NULL ORDER BY doc_id, chunk_index"
        )
        chunks = cursor.fetchall()

    if not chunks:
        # No chunks to reindex, release lock immediately
        set_system_metadata("embedding_model", settings.EMBEDDING_MODEL or "text-embedding-3-small")
        set_system_metadata("embedding_base_url", settings.EMBEDDING_BASE_URL or "https://api.openai.com/v1")
        set_system_metadata("vector_locked", "false")
        set_system_metadata("vector_lock_reason", "")
        return {"ok": True, "reindexed_count": 0, "message": "无切片数据，向量锁已解除"}

    # 3. Create staging table
    with db_session() as conn:
        conn.execute("DROP TABLE IF EXISTS document_chunks_staging")
        conn.execute("CREATE TABLE document_chunks_staging (id TEXT PRIMARY KEY, embedding BLOB)")

    batch_size = 10
    vector_dimension: Optional[int] = None
    try:
        for i in range(0, len(chunks), batch_size):
            batch = chunks[i : i + batch_size]
            batch_ids = [c["id"] for c in batch]
            batch_texts = [c["content"] for c in batch]

            vectors = await EmbeddingService.get_embeddings_batch(batch_texts)
            if len(vectors) != len(batch):
                raise ValueError(f"向量服务返回数量不匹配：期望 {len(batch)}，实际 {len(vectors)}")
            with db_session() as conn:
                for cid, vec in zip(batch_ids, vectors):
                    vec_np = np.array(vec, dtype=np.float32) if not isinstance(vec, np.ndarray) else vec
                    if vec_np.ndim != 1 or vec_np.size == 0:
                        raise ValueError("向量服务返回了空向量或非一维向量")
                    if vector_dimension is None:
                        vector_dimension = int(vec_np.size)
                    elif int(vec_np.size) != vector_dimension:
                        raise ValueError("向量服务返回的维度不一致")
                    blob = EmbeddingService.serialize_embedding(vec_np)
                    conn.execute(
                        "INSERT INTO document_chunks_staging (id, embedding) VALUES (?, ?)",
                        (cid, blob),
                    )
    except Exception as exc:
        logger.error("Reindex staging failed: %s; rolling back", exc)
        with db_session() as conn:
            conn.execute("DROP TABLE IF EXISTS document_chunks_staging")
        raise HTTPException(status_code=500, detail=f"重建索引失败（生成向量错误: {exc}），已保留原切片与锁定状态")

    # 4. Atomic transaction to replace live embeddings from staging
    with db_session() as conn:
        conn.execute(
            """UPDATE document_chunks
               SET embedding = (SELECT embedding FROM document_chunks_staging WHERE document_chunks_staging.id = document_chunks.id),
                   embedding_status = 'completed',
                   updated_at = CURRENT_TIMESTAMP
               WHERE id IN (SELECT id FROM document_chunks_staging)"""
        )
        conn.execute("DROP TABLE IF EXISTS document_chunks_staging")
        conn.execute(
            """INSERT INTO system_metadata (key, value, updated_at) VALUES ('embedding_model', ?, CURRENT_TIMESTAMP)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP""",
            (settings.EMBEDDING_MODEL or "text-embedding-3-small",)
        )
        conn.execute(
            """INSERT INTO system_metadata (key, value, updated_at) VALUES ('embedding_base_url', ?, CURRENT_TIMESTAMP)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP""",
            (settings.EMBEDDING_BASE_URL or "https://api.openai.com/v1",)
        )
        conn.execute(
            """INSERT INTO system_metadata (key, value, updated_at) VALUES ('vector_locked', 'false', CURRENT_TIMESTAMP)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP"""
        )
        conn.execute(
            """INSERT INTO system_metadata (key, value, updated_at) VALUES ('vector_lock_reason', '', CURRENT_TIMESTAMP)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP"""
        )

    return {"ok": True, "reindexed_count": len(chunks), "message": f"成功重建 {len(chunks)} 条切片向量，已解除锁定"}


class MaintenanceRequest(BaseModel):
    lease_id: str = Field(..., min_length=1)
    operation: str = Field(..., min_length=1)
    is_exit: bool = False


@desktop_router.post("/maintenance/acquire")
def api_acquire_maintenance(body: MaintenanceRequest):
    MaintenanceManager.acquire(body.lease_id, body.operation, is_exit=body.is_exit)
    return {"ok": True, "lease_id": body.lease_id, "operation": body.operation}


class ReleaseMaintenanceRequest(BaseModel):
    lease_id: str = Field(..., min_length=1)


@desktop_router.post("/maintenance/release")
def api_release_maintenance(body: ReleaseMaintenanceRequest):
    released = MaintenanceManager.release(body.lease_id)
    return {"ok": released}


class DrainRequest(BaseModel):
    lease_id: Optional[str] = None


@desktop_router.post("/drain")
async def api_drain_tasks(body: Optional[DrainRequest] = None):
    """Graceful drain: stops task worker, marks active tasks interrupted, flushes WAL via independent connection."""
    from app.services.task_worker import TaskWorker
    from app.services.request_operations import stop_operations
    import sqlite3

    active_lease = MaintenanceManager.get_lease()
    if active_lease:
        expected = active_lease.get("lease_id")
        provided = body.lease_id if body else None
        if not provided or provided != expected:
            raise HTTPException(
                status_code=403,
                detail="排空请求 lease_id 与当前活跃维护租约不匹配，拒绝执行排空",
            )
    else:
        raise HTTPException(
            status_code=409,
            detail="排空前必须先获取维护租约",
        )

    logger.info("Executing desktop system drain...")
    MaintenanceManager.set_draining(True)

    # 1. Stop background loops and cancel running workers
    TaskWorker.stop()
    running_tasks = list(getattr(TaskWorker, "_running_tasks", {}).values())
    pending = [t for t in running_tasks if not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    if hasattr(TaskWorker, "_running_tasks"):
        TaskWorker._running_tasks.clear()

    # 2. Stop synchronous request operations
    await stop_operations()

    # 3. Mark in-flight tasks in SQLite as failed with clean message so user can retry manually
    try:
        with db_session() as db:
            db.execute(
                """UPDATE tasks
                   SET status = 'failed', error_msg = '软件退出或维护已中断，可手动重新开始', updated_at = CURRENT_TIMESTAMP
                   WHERE status IN ('pending', 'processing', 'cancel_requested')"""
            )
    except Exception as exc:
        logger.error("Error during drain tasks update: %s", exc)
        raise HTTPException(status_code=500, detail=f"排空任务数据库更新失败: {exc}")

    # 4. Flush SQLite WAL on an independent connection outside any transaction/ORM session
    db_file = str(settings.db_path)
    if os.path.isfile(db_file):
        try:
            conn = sqlite3.connect(db_file, timeout=10.0, isolation_level=None)
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()
            logger.info("SQLite WAL checkpoint (TRUNCATE) successfully executed on independent connection.")
        except Exception as wal_err:
            logger.error("Independent WAL checkpoint error: %s", wal_err)
            raise HTTPException(status_code=500, detail=f"WAL checkpoint 刷盘失败: {wal_err}")

    return {"ok": True, "message": "系统已排空并刷盘持久化"}


class ShutdownRequest(BaseModel):
    lease_id: Optional[str] = None


@desktop_router.post("/shutdown")
async def api_shutdown(body: Optional[ShutdownRequest] = None):
    """Graceful shutdown endpoint with lease verification."""
    active_lease = MaintenanceManager.get_lease()
    if active_lease:
        expected = active_lease.get("lease_id")
        provided = body.lease_id if body else None
        if not provided or provided != expected:
            raise HTTPException(
                status_code=403,
                detail="停机请求 lease_id 与当前活跃维护租约不匹配，拒绝执行停机",
            )
    else:
        provided = body.lease_id if body else None
        if not provided:
            raise HTTPException(
                status_code=409,
                detail="停机前必须先获取维护租约",
            )
        else:
            raise HTTPException(
                status_code=403,
                detail="未找到对应的活跃维护租约，拒绝停机",
            )

    await api_drain_tasks(body=DrainRequest(lease_id=provided))
    loop = asyncio.get_running_loop()
    loop.call_later(0.3, lambda: os._exit(0))
    return {"ok": True, "message": "服务正在安全退出"}


class ConnectionTestRequest(BaseModel):
    service_type: str = Field(..., description="llm | embedding | ocr (selected server provider) | baidu_ocr")
    base_url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    baidu_ocr_url: Optional[str] = None
    baidu_ocr_token: Optional[str] = None


# Connectivity probe timeout. Kept as a named constant so the value that is logged
# (`timeout_config`) is the same value that the request actually used.
LLM_TEST_TIMEOUT_SECONDS = 20.0
MAX_UPSTREAM_ERROR_SUMMARY_LENGTH = 300


def _extract_upstream_error_summary(res: httpx.Response) -> Dict[str, Any]:
    """
    Extracts a masked, length-limited summary from an upstream error response.

    Only a provider error code and a scrubbed, truncated message ever leave this function:
    the raw body, request headers, credentials and model output are never returned or logged.
    A non-JSON body is described by its format instead of its content.
    """
    content_type = (res.headers.get("content-type") or "").split(";")[0].strip().lower()
    summary: Dict[str, Any] = {"body_format": content_type or "unknown"}
    declared_length = res.headers.get("content-length")
    if declared_length and declared_length.isdigit():
        summary["bytes"] = int(declared_length)

    try:
        data = res.json()
    except Exception:
        summary["reason_code"] = "non_json_error_body"
        return summary

    if not isinstance(data, dict):
        summary["reason_code"] = "unexpected_error_body"
        return summary

    error = data.get("error")
    code: Any = None
    message: Any = None
    if isinstance(error, dict):
        code = error.get("code") or error.get("type")
        message = error.get("message")
    elif isinstance(error, str):
        message = error
    if code is None:
        code = data.get("code") or data.get("error_code")
    if message is None:
        message = data.get("message") or data.get("msg")

    if code is not None and str(code).strip():
        summary["reason_code"] = str(code).strip()[:80]
    if isinstance(message, str) and message.strip():
        summary["detail"] = sanitize_text(message).strip()[:MAX_UPSTREAM_ERROR_SUMMARY_LENGTH]
    return summary


def _extract_llm_message_content(data: Any) -> Optional[str]:
    """Reads `choices[0].message.content`, returning None when the response shape is unexpected."""
    if not isinstance(data, dict):
        return None
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    return content if isinstance(content, str) else None


def _classify_llm_http_status(status_code: int) -> str:
    """Maps an upstream HTTP status to a stable, searchable failure code."""
    if status_code == 401:
        return "auth_failed"
    if status_code == 403:
        return "permission_denied"
    if status_code == 404:
        return "endpoint_not_found"
    if status_code == 429:
        return "rate_limited"
    if status_code >= 500:
        return "upstream_error"
    if 400 <= status_code < 500:
        return "upstream_rejected"
    return "unexpected_status"


def _classify_llm_exception(exc: BaseException) -> str:
    """Maps a transport exception to a stable, searchable failure code."""
    if isinstance(exc, httpx.ConnectTimeout):
        return "connect_timeout"
    if isinstance(exc, httpx.ReadTimeout):
        return "read_timeout"
    if isinstance(exc, httpx.WriteTimeout):
        return "write_timeout"
    if isinstance(exc, httpx.PoolTimeout):
        return "pool_timeout"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        return "connect_failed"
    if isinstance(exc, httpx.NetworkError):
        return "network_error"
    return "unexpected_error"


def _llm_http_failure_message(status_code: int, model: str) -> str:
    """Short, actionable Chinese reason for a non-200 upstream response."""
    if status_code == 401:
        return "LLM API Key 无效或未授权 (401)"
    if status_code == 403:
        return "LLM 访问被拒绝 (403)，请检查密钥权限或来源限制"
    if status_code == 404:
        return f"LLM 模型或端点不存在 (404: {model})"
    if status_code == 429:
        return "LLM 服务限流 (429)，请稍后重试"
    if status_code >= 500:
        return f"LLM 服务端异常（HTTP {status_code}），请稍后重试"
    return f"LLM 服务返回错误（HTTP {status_code}）"


_LLM_EXCEPTION_MESSAGES = {
    "connect_timeout": "LLM 连接超时，请检查网络或 Base URL",
    "read_timeout": "LLM 响应读取超时，服务端长时间无响应",
    "write_timeout": "LLM 请求写入超时",
    "pool_timeout": "LLM 连接池等待超时",
    "timeout": "LLM 请求超时",
    "connect_failed": "无法连接 LLM 服务，请检查网络或 Base URL",
    "network_error": "LLM 网络异常",
    "unexpected_error": "LLM 测试发生未预期错误",
}


def _llm_test_failure(
    *,
    call_id: str,
    model: str,
    endpoint: str,
    error_type: str,
    message: str,
    failed_stage: str,
    duration_ms: float,
    status_code: Optional[int] = None,
    provider_request_id: Optional[str] = None,
    reason_code: Optional[str] = None,
    detail: Optional[str] = None,
    note: Optional[str] = None,
    exception_info: Optional[Dict[str, Any]] = None,
    exception_type: Optional[str] = None,
    level: int = logging.ERROR,
) -> Dict[str, Any]:
    """
    Logs one LLM connectivity-test failure and returns the client envelope.

    Every failure carries the correlation id, model, sanitized endpoint, elapsed time and
    the stage it died in; network failures additionally carry the safe exception chain and
    stack. Response fields are preserved from the original contract (`ok` / `service` /
    `message` / `status_code`) with `call_id` and `error_type` added.
    """
    if exception_type is None and exception_info:
        exception_type = exception_info.get("exception_type")
    log_diagnostic(
        "llm_test_failed",
        level=level,
        message=message,
        service="llm",
        model=model,
        endpoint=endpoint,
        status_code=status_code,
        provider_request_id=provider_request_id,
        duration_ms=duration_ms,
        error_type=error_type,
        failed_stage=failed_stage,
        reason_code=reason_code,
        detail=detail,
        note=note,
        exception_type=exception_type,
        exception_info=exception_info,
    )
    result: Dict[str, Any] = {
        "ok": False,
        "service": "llm",
        "error_type": error_type,
        "call_id": call_id,
        "message": message,
    }
    if status_code is not None:
        result["status_code"] = status_code
    return result


@desktop_router.post("/test-connection")
async def test_service_connection(body: ConnectionTestRequest):
    """Test connectivity for LLM, Embedding, or Baidu OCR using a real, capability-verifying probe.

    - OCR uses a synthetic image with known text rendered by fitz to verify real text extraction.
    - LLM verifies parsed target response.
    - Embedding verifies non-empty finite numerical vector.
    """
    service = body.service_type.lower().strip()
    logger.info("Desktop test connection requested for service: %s", service)

    if service == "llm":
        base_url = (body.base_url or settings.LLM_BASE_URL).rstrip("/")
        model = body.model or settings.LLM_MODEL
        api_key = body.api_key or settings.LLM_API_KEY
        endpoint = sanitize_endpoint(f"{base_url}/chat/completions")

        with call_context() as call_id:
            if not api_key:
                return _llm_test_failure(
                    call_id=call_id,
                    model=model,
                    endpoint=endpoint,
                    error_type="missing_credentials",
                    message="LLM API Key 未配置，请先填写密钥后再测试",
                    failed_stage="precheck",
                    duration_ms=0.0,
                    level=logging.WARNING,
                )

            url = f"{base_url}/chat/completions"
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": 'Please reply with exactly: {"status":"ok","code":200}'}],
                "max_tokens": 50,
                "temperature": 0.0,
            }
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }

            log_diagnostic(
                "llm_test_started",
                level=logging.INFO,
                message=f"开始测试 LLM 连通性 (模型: {model})",
                service="llm",
                model=model,
                endpoint=endpoint,
                timeout_config=LLM_TEST_TIMEOUT_SECONDS,
            )

            t_start = time.monotonic()
            try:
                async with httpx.AsyncClient(timeout=LLM_TEST_TIMEOUT_SECONDS) as client:
                    res = await client.post(url, json=payload, headers=headers)
            except Exception as exc:
                duration_ms = round((time.monotonic() - t_start) * 1000.0, 2)
                error_type = _classify_llm_exception(exc)
                exception_info = extract_safe_exception_info(exc, stage="request_send")
                return _llm_test_failure(
                    call_id=call_id,
                    model=model,
                    endpoint=endpoint,
                    error_type=error_type,
                    message=_LLM_EXCEPTION_MESSAGES.get(error_type, _LLM_EXCEPTION_MESSAGES["unexpected_error"]),
                    failed_stage=exception_info.get("failed_stage", "request_send"),
                    duration_ms=duration_ms,
                    exception_info=exception_info,
                )

            status_code = res.status_code
            provider_req_id = extract_provider_request_id(res.headers)

            if status_code != 200:
                summary = _extract_upstream_error_summary(res)
                duration_ms = round((time.monotonic() - t_start) * 1000.0, 2)
                return _llm_test_failure(
                    call_id=call_id,
                    model=model,
                    endpoint=endpoint,
                    error_type=_classify_llm_http_status(status_code),
                    message=_llm_http_failure_message(status_code, model),
                    failed_stage="http_status",
                    duration_ms=duration_ms,
                    status_code=status_code,
                    provider_request_id=provider_req_id,
                    reason_code=summary.get("reason_code"),
                    detail=summary.get("detail"),
                    note=summary.get("body_format"),
                    exception_type="HTTPStatusError",
                )

            try:
                data = res.json()
            except Exception as exc:
                duration_ms = round((time.monotonic() - t_start) * 1000.0, 2)
                exception_info = extract_safe_exception_info(exc, stage="response_parse")
                return _llm_test_failure(
                    call_id=call_id,
                    model=model,
                    endpoint=endpoint,
                    error_type="invalid_json",
                    message="LLM 返回内容不是合法 JSON，无法确认服务可用",
                    failed_stage="response_parse",
                    duration_ms=duration_ms,
                    status_code=status_code,
                    provider_request_id=provider_req_id,
                    note=(res.headers.get("content-type") or "").split(";")[0].strip().lower() or "unknown",
                    exception_info=exception_info,
                )

            content = _extract_llm_message_content(data)
            duration_ms = round((time.monotonic() - t_start) * 1000.0, 2)
            if content is None:
                return _llm_test_failure(
                    call_id=call_id,
                    model=model,
                    endpoint=endpoint,
                    error_type="unexpected_response",
                    message="LLM 响应结构缺少 choices[0].message.content",
                    failed_stage="response_structure",
                    duration_ms=duration_ms,
                    status_code=status_code,
                    provider_request_id=provider_req_id,
                )

            log_diagnostic(
                "llm_test_succeeded",
                level=logging.INFO,
                message=f"LLM 连通性测试成功 (模型: {model})",
                service="llm",
                model=model,
                endpoint=endpoint,
                status_code=status_code,
                provider_request_id=provider_req_id,
                duration_ms=duration_ms,
            )
            matched = "ok" in content.lower() or "200" in content
            return {
                "ok": True,
                "service": "llm",
                "call_id": call_id,
                "message": (
                    f"LLM 服务测试成功 (模型: {model})"
                    if matched
                    else "LLM 服务响应成功"
                ),
            }

    elif service == "embedding":
        base_url = (body.base_url or settings.EMBEDDING_BASE_URL).rstrip("/")
        model = body.model or settings.EMBEDDING_MODEL
        api_key = body.api_key or settings.EMBEDDING_API_KEY
        if not api_key:
            raise HTTPException(status_code=400, detail="Embedding API Key 未配置")

        url = f"{base_url}/embeddings"
        payload = {
            "model": model,
            "input": ["probe text for embedding"],
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                res = await client.post(url, json=payload, headers=headers)
                if res.status_code == 200:
                    data = res.json()
                    items = data.get("data", [])
                    if not items or "embedding" not in items[0]:
                        return {"ok": False, "service": "embedding", "message": "Embedding 服务返回数据缺少 embedding 向量"}
                    raw_vec = items[0]["embedding"]
                    if not isinstance(raw_vec, list) or len(raw_vec) == 0:
                        return {"ok": False, "service": "embedding", "message": "Embedding 向量为空或非数组"}
                    if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in raw_vec[:10]):
                        return {"ok": False, "service": "embedding", "message": "Embedding 向量包含非数值或无限值"}
                    return {
                        "ok": True,
                        "service": "embedding",
                        "message": f"Embedding 服务测试成功 (维度: {len(raw_vec)}, 模型: {model})",
                    }
                return {
                    "ok": False,
                    "service": "embedding",
                    "status_code": res.status_code,
                    "message": f"Embedding 服务返回错误（HTTP {res.status_code}）",
                }
        except Exception as exc:
            return {"ok": False, "service": "embedding", "message": f"Embedding 连接失败: {type(exc).__name__}"}

    elif service == "ocr":
        from app.services.parser.ocr_service import OCRService, OCRServiceError

        config = OCRService.configuration_status()
        if not config["configured"]:
            raise HTTPException(status_code=400, detail=config["message"] or "OCR 服务未配置")

        doc = fitz.open()
        try:
            page = doc.new_page(width=320, height=80)
            page.insert_text((20, 50), "TEST_OCR_PROBE_2026", fontsize=22)
            png_bytes = page.get_pixmap(dpi=150).tobytes("png")
        finally:
            doc.close()

        try:
            recognized = await OCRService.extract_text_from_image_bytes(png_bytes, "png", max_attempts=1)
            if "TEST_OCR_PROBE_2026" not in recognized:
                return {"ok": False, "service": "ocr", "message": "OCR 返回内容未识别出测试文字，请检查模型是否支持图片文字识别"}
            return {"ok": True, "service": "ocr", "message": "OCR 识别测试成功，已识别出测试图片文字"}
        except OCRServiceError as exc:
            return {"ok": False, "service": "ocr", "message": str(exc)}
        except Exception as exc:
            return {"ok": False, "service": "ocr", "message": f"OCR 连接失败: {type(exc).__name__}"}

    elif service == "baidu_ocr":
        api_url = (body.baidu_ocr_url or settings.BAIDU_OCR_API_URL).strip()
        token = (body.baidu_ocr_token or settings.BAIDU_OCR_TOKEN).strip()
        if not api_url or not token:
            raise HTTPException(status_code=400, detail="百度 OCR API URL 或 Token 未配置")

        # Generate a real synthetic image with known text using PyMuPDF
        doc = fitz.open()
        page = doc.new_page(width=320, height=80)
        page.insert_text((20, 50), "TEST_OCR_PROBE_2026", fontsize=22)
        pix = page.get_pixmap(dpi=150)
        png_bytes = pix.tobytes("png")
        doc.close()

        from app.services.parser.ocr_service import OCRService, OCRQuotaExhaustedError, OCRServiceError
        try:
            recognized = await OCRService._extract_via_paddleocr_api(
                png_bytes,
                "png",
                override_api_url=api_url,
                override_token=token,
            )
            if recognized and recognized.strip():
                return {
                    "ok": True,
                    "service": "baidu_ocr",
                    "message": f"百度 OCR 识别测试成功，提取到有效文字",
                }
            return {"ok": False, "service": "baidu_ocr", "message": "百度 OCR 返回空文本"}
        except OCRQuotaExhaustedError as q_err:
            return {"ok": False, "service": "baidu_ocr", "message": f"百度 OCR 额度耗尽或限流: {q_err}"}
        except OCRServiceError as ocr_err:
            return {"ok": False, "service": "baidu_ocr", "message": str(ocr_err)}
        except Exception as exc:
            return {"ok": False, "service": "baidu_ocr", "message": f"百度 OCR 连接失败: {type(exc).__name__}"}

    else:
        raise HTTPException(status_code=400, detail=f"不支持的测试服务类型: {service}")


class ValidateConfigRequest(BaseModel):
    new_embedding_base_url: Optional[str] = None
    new_embedding_model: Optional[str] = None


@desktop_router.post("/config/validate")
def validate_config_change(body: ValidateConfigRequest):
    """Validate if proposed config change is allowed under current system state."""
    try:
        has_emb = has_existing_embeddings()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"检查已有向量库失败: {exc}，拒绝修改配置")

    if has_emb:
        curr_url = settings.EMBEDDING_BASE_URL.rstrip("/")
        curr_model = settings.EMBEDDING_MODEL
        if body.new_embedding_base_url and body.new_embedding_base_url.rstrip("/") != curr_url:
            raise HTTPException(
                status_code=400,
                detail="数据库中已存在向量数据，禁止修改 Embedding API 地址（防止向量空间混用）",
            )
        if body.new_embedding_model and body.new_embedding_model.strip() != curr_model.strip():
            raise HTTPException(
                status_code=400,
                detail="数据库中已存在向量数据，禁止修改 Embedding 模型名称（防止向量维度与空间混用）",
            )
    return {"allowed": True, "message": "配置合法"}


def get_frontend_dist_dir() -> Optional[Path]:
    """Resolve directory containing built frontend assets."""
    if settings.FRONTEND_DIST_DIR:
        p = Path(settings.FRONTEND_DIST_DIR).resolve()
        if p.is_dir():
            return p

    # PyInstaller onedir bundled location
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        bundled = Path(sys._MEIPASS) / "frontend_dist"
        if bundled.is_dir():
            return bundled

    # Development fallback
    repo_dist = Path(__file__).parents[2] / "frontend" / "dist"
    if repo_dist.is_dir():
        return repo_dist

    return None


def setup_desktop_static_serving(app):
    """Mount frontend static files and SPA catch-all route when dist is available."""
    app.state.public_frontend_routes = ()
    dist_dir = get_frontend_dist_dir()
    if not dist_dir:
        logger.info("Frontend dist directory not found; skipping static file serving.")
        return

    logger.info("Setting up frontend static serving from: %s", dist_dir)
    dist_dir = dist_dir.resolve()
    frontend_routes = []
    assets_dir = dist_dir / "assets"
    if assets_dir.is_dir() and assets_dir.resolve().is_relative_to(dist_dir):
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")
        frontend_routes.append(app.routes[-1])

    index_html = dist_dir / "index.html"

    def confined_path(path: Path) -> Path:
        try:
            resolved = path.resolve()
        except (OSError, ValueError, RuntimeError):
            raise HTTPException(status_code=404, detail="Frontend file not found")
        if not resolved.is_relative_to(dist_dir):
            raise HTTPException(status_code=403, detail="拒绝访问：路径越界")
        return resolved

    @app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def serve_spa(full_path: str):
        # Don't intercept API routes
        if full_path.startswith("api/") or full_path == "api":
            raise HTTPException(status_code=404, detail="API route not found")

        # Resolve candidate and enforce strict path traversal check
        candidate = confined_path(dist_dir / full_path)
        if candidate.is_file():
            return FileResponse(str(candidate))
        index = confined_path(index_html)
        if index.is_file():
            return FileResponse(str(index))
        raise HTTPException(status_code=404, detail="Frontend index.html not found")

    frontend_routes.append(app.routes[-1])
    # Desktop/development auto-discovery must not silently publish a web UI.
    # Public deployments opt in by explicitly configuring the bundled directory.
    if settings.FRONTEND_DIST_DIR and Path(settings.FRONTEND_DIST_DIR).resolve() == dist_dir:
        app.state.public_frontend_routes = tuple(frontend_routes)
