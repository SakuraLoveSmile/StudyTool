import asyncio
import json
import logging
import math
import re
import uuid
from pathlib import Path
from typing import Optional, Dict, Any, Callable
from app.config import settings
from app.database import (db_session, current_embedding_source, assert_embedding_source,
                          record_embedding_source)
from app.services.parser.pdf_parser import PDFParser
from app.services.parser.docx_parser import DocxParser
from app.services.parser.image_parser import ImageParser
from app.services.parser.text_splitter import segment_chinese
from app.services.retrieval.embedding import EmbeddingService
from app.services.llm.client import LLMClient
from app.services.llm.prompts import QUESTION_EXTRACT_SYSTEM_PROMPT, STUDY_CARD_SYSTEM_PROMPT
from app.services.retrieval.hybrid import HybridSearchService
from app.diagnostics import (
    task_context,
    unit_context,
    set_unit_context,
    reset_unit_context,
    log_diagnostic,
    extract_safe_exception_info,
)

import time
from contextlib import contextmanager, nullcontext
from datetime import datetime

logger = logging.getLogger(__name__)

# Unit states considered still active (a request/step in flight)
ACTIVE_UNIT_STATES = {"queued", "waiting_slot", "awaiting_response", "processing", "saving", "retry_wait"}
# Terminal unit states
TERMINAL_UNIT_STATES = {"succeeded", "failed", "skipped", "cancelled"}
SERVICE_LABELS = {"llm": "模型", "ocr": "OCR", "embedding": "向量服务"}
# Keep at most this many finished units in progress_detail_json (active units are always kept)
MAX_FINISHED_UNITS = 15


def _current_tenant_id() -> Optional[str]:
    """Read tenancy lazily so the worker remains usable in legacy mode/tests."""
    try:
        from app.tenancy import current_tenant_id
        return current_tenant_id()
    except (ImportError, AttributeError):
        return None


def _public_mode() -> bool:
    return bool(getattr(settings, "PUBLIC_MODE", False))


@contextmanager
def _tenant_scope(tenant_id: str):
    from app.tenancy import tenant_scope
    with tenant_scope(tenant_id):
        yield


class _TaskWorkerMeta(type):
    """Dispatch the legacy class API to the worker belonging to the request tenant."""

    _tenant_workers: Dict[str, type] = {}
    _control_attrs = {
        "_tenant_marker", "_tenant_workers", "_worker_for_tenant",
        "_tenant_manager_task", "_tenant_manager_loop", "_stop_worker",
        "start_all", "stop_all",
        "__dict__", "__class__",
    }

    def __getattribute__(cls, name: str):
        if type.__getattribute__(cls, "_tenant_marker") or name in _TaskWorkerMeta._control_attrs:
            return super().__getattribute__(name)
        tenant_id = _current_tenant_id()
        if tenant_id is None:
            if _public_mode():
                raise RuntimeError("TaskWorker requires an authenticated tenant context")
            return super().__getattribute__(name)
        worker = type.__getattribute__(cls, "_worker_for_tenant")(tenant_id)
        return type.__getattribute__(worker, name)


def utc_now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


class TaskWorker(metaclass=_TaskWorkerMeta):
    _tenant_marker = False
    _instance: Optional["TaskWorker"] = None
    _running: bool = False
    _worker_task: Optional[asyncio.Task] = None
    _heartbeat_task: Optional[asyncio.Task] = None
    _running_tasks: Dict[str, asyncio.Task] = {}
    _task_details: Dict[str, Dict[str, Any]] = {}
    _task_timings: Dict[str, Dict[str, Any]] = {}
    _last_db_flush: Dict[str, float] = {}
    _task_start_mono: Dict[str, float] = {}
    _tenant_manager_task: Optional[asyncio.Task] = None

    @classmethod
    def _worker_for_tenant(cls, tenant_id: str) -> type:
        worker = _TaskWorkerMeta._tenant_workers.get(tenant_id)
        if worker is None:
            worker = _TaskWorkerMeta(
                f"TaskWorker_{tenant_id.replace('-', '_')}",
                (cls,),
                {
                    "_tenant_marker": True,
                    "_tenant_id": tenant_id,
                    "_instance": None,
                    "_running": False,
                    "_worker_task": None,
                    "_heartbeat_task": None,
                    "_running_tasks": {},
                    "_task_details": {},
                    "_task_timings": {},
                    "_last_db_flush": {},
                    "_task_start_mono": {},
                },
            )
            _TaskWorkerMeta._tenant_workers[tenant_id] = worker
        return worker

    @classmethod
    def start_all(cls):
        """Start one serial worker per enabled account and watch account changes."""
        if not _public_mode():
            cls.start()
            return
        if cls._tenant_manager_task is None or cls._tenant_manager_task.done():
            cls._tenant_manager_task = asyncio.create_task(cls._tenant_manager_loop())

    @classmethod
    async def stop_all(cls):
        manager = cls._tenant_manager_task
        cls._tenant_manager_task = None
        if manager and not manager.done():
            manager.cancel()
            await asyncio.gather(manager, return_exceptions=True)
        workers = list(_TaskWorkerMeta._tenant_workers.values())
        await asyncio.gather(*(cls._stop_worker(worker) for worker in workers), return_exceptions=True)
        _TaskWorkerMeta._tenant_workers.clear()

    @classmethod
    async def _stop_worker(cls, worker: type):
        with _tenant_scope(worker._tenant_id):
            worker.stop()
            pending = [
                task for task in (
                    worker._worker_task,
                    worker._heartbeat_task,
                    *worker._running_tasks.values(),
                ) if task and not task.done()
            ]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            worker._worker_task = None
            worker._heartbeat_task = None
            worker._running_tasks.clear()

    @classmethod
    async def _tenant_manager_loop(cls):
        while True:
            try:
                from app.auth import enabled_user_ids
                result = enabled_user_ids()
                if asyncio.iscoroutine(result):
                    result = await result
                enabled = {str(user_id) for user_id in result}
                for user_id in enabled:
                    worker = cls._worker_for_tenant(user_id)
                    if not worker._running:
                        with _tenant_scope(user_id):
                            worker.start()
                for user_id, worker in list(_TaskWorkerMeta._tenant_workers.items()):
                    if user_id not in enabled and worker._running:
                        with _tenant_scope(user_id):
                            await cls._stop_worker(worker)
                        from app.services.request_operations import stop_tenant_operations
                        await stop_tenant_operations(user_id)
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("TaskWorker tenant manager iteration failed")
                await asyncio.sleep(1.0)

    @classmethod
    def start(cls):
        if not cls._running:
            cls._running = True
            cls._worker_task = asyncio.create_task(cls._run_loop())
            cls._heartbeat_task = asyncio.create_task(cls._heartbeat_loop())
            logger.info("TaskWorker background loop and heartbeat loop started")

    @classmethod
    def stop(cls):
        cls._running = False
        if cls._worker_task:
            cls._worker_task.cancel()
        if cls._heartbeat_task:
            cls._heartbeat_task.cancel()
        for t in list(cls._running_tasks.values()):
            if not t.done():
                t.cancel()

    @classmethod
    def create_task(
        cls,
        task_type: str,
        target_id: Optional[str] = None,
        message: str = "已进入队列",
        parent_task_id: Optional[str] = None,
        conn: Optional[Any] = None,
    ) -> str:
        task_id = str(uuid.uuid4())
        initial_detail = {
            "stage": "init",
            "phase": "working",
            "unit": "batch",
            "total": None,
            "succeeded": 0,
            "failed": 0,
            "skipped": 0,
            "activeUnits": [],
            "resultCount": 0,
            "attempt": 1,
            "currentScope": None,
            "currentAction": message,
            "requestStartedAt": None,
            "heartbeatAt": utc_now_iso(),
            "lastProgressAt": None,
            "execUnits": [],
            "summary": None
        }
        initial_timings = {
            "local_parse": {"wall_time_ms": 0.0, "elapsed_time_ms": 0.0, "count": 0},
            "ocr_request": {"wall_time_ms": 0.0, "elapsed_time_ms": 0.0, "count": 0},
            "llm_extract_request": {"wall_time_ms": 0.0, "elapsed_time_ms": 0.0, "count": 0},
            "embedding_request": {"wall_time_ms": 0.0, "elapsed_time_ms": 0.0, "count": 0},
            "validation": {"wall_time_ms": 0.0, "elapsed_time_ms": 0.0, "count": 0},
            "db_write": {"wall_time_ms": 0.0, "elapsed_time_ms": 0.0, "count": 0},
            "retry_wait": {"wall_time_ms": 0.0, "elapsed_time_ms": 0.0, "count": 0},
        }
        cls._task_details[task_id] = initial_detail
        cls._task_timings[task_id] = initial_timings

        def _insert(active_conn: Any) -> None:
            cursor = active_conn.cursor()
            cursor.execute("""
            INSERT INTO tasks (
                id, task_type, target_id, status, progress, message,
                parent_task_id, progress_detail_json, timing_breakdown_json
            )
            VALUES (?, ?, ?, 'pending', 0, ?, ?, ?, ?)
            """, (
                task_id, task_type, target_id, message,
                parent_task_id,
                json.dumps(initial_detail, ensure_ascii=False),
                json.dumps(initial_timings, ensure_ascii=False)
            ))

        if conn is not None:
            _insert(conn)
        else:
            with db_session() as active_conn:
                _insert(active_conn)
        # Keep task/batch creation atomic when the caller provides a connection.
        cls.log_event(task_id, "created", f"任务已创建并进入队列: {message}", stage="init", conn=conn)
        return task_id

    @classmethod
    def log_event(
        cls,
        task_id: str,
        event_type: str,
        message: str,
        stage: Optional[str] = None,
        unit_index: Optional[int] = None,
        data: Optional[Dict[str, Any]] = None,
        conn: Optional[Any] = None
    ):
        """Records task event for incremental timeline and diagnostics."""
        try:
            if conn is not None:
                conn.execute("""
                    INSERT INTO task_events (task_id, event_type, stage, unit_index, message, data_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    task_id, event_type, stage, unit_index, message,
                    json.dumps(data, ensure_ascii=False) if data else None
                ))
            else:
                with db_session() as c:
                    c.execute("""
                        INSERT INTO task_events (task_id, event_type, stage, unit_index, message, data_json)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        task_id, event_type, stage, unit_index, message,
                        json.dumps(data, ensure_ascii=False) if data else None
                    ))
        except Exception as exc:
            logger.warning("Failed to log task event for %s: %s", task_id, exc)

    @classmethod
    def record_timing(
        cls,
        task_id: str,
        category: str,
        elapsed_ms: float,
        wall_ms: Optional[float] = None
    ):
        """Records granular timing breakdown for a task category."""
        timings = cls._task_timings.setdefault(task_id, {
            "local_parse": {"wall_time_ms": 0.0, "elapsed_time_ms": 0.0, "count": 0},
            "ocr_request": {"wall_time_ms": 0.0, "elapsed_time_ms": 0.0, "count": 0},
            "llm_extract_request": {"wall_time_ms": 0.0, "elapsed_time_ms": 0.0, "count": 0},
            "embedding_request": {"wall_time_ms": 0.0, "elapsed_time_ms": 0.0, "count": 0},
            "validation": {"wall_time_ms": 0.0, "elapsed_time_ms": 0.0, "count": 0},
            "db_write": {"wall_time_ms": 0.0, "elapsed_time_ms": 0.0, "count": 0},
            "retry_wait": {"wall_time_ms": 0.0, "elapsed_time_ms": 0.0, "count": 0},
        })
        cat = timings.setdefault(category, {"wall_time_ms": 0.0, "elapsed_time_ms": 0.0, "count": 0})
        cat["count"] += 1
        cat["elapsed_time_ms"] += round(elapsed_ms, 2)
        cat["wall_time_ms"] += round(wall_ms if wall_ms is not None else elapsed_ms, 2)

    @staticmethod
    def sanitize_error(msg: Any) -> Optional[str]:
        """Masks potential secrets and collapses an error message for safe display."""
        if msg is None:
            return None
        text = str(msg)
        text = re.sub(r"(sk-[A-Za-z0-9_-]{4})[A-Za-z0-9_-]+", r"\1***", text)
        text = re.sub(r"(?i)(bearer\s+)\S+", r"\1***", text)
        text = re.sub(r"(?i)((?:api[_-]?key|access[_-]?token|token|authorization)(?:\s*[=:]\s*))\S+", r"\1***", text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > 200:
            text = text[:197] + "..."
        return text or None

    @classmethod
    def _get_detail(cls, task_id: str) -> Dict[str, Any]:
        return cls._task_details.setdefault(task_id, {
            "stage": "init",
            "phase": "working",
            "unit": "batch",
            "total": None,
            "succeeded": 0,
            "failed": 0,
            "skipped": 0,
            "activeUnits": [],
            "resultCount": 0,
            "attempt": 1,
            "currentScope": None,
            "currentAction": None,
            "requestStartedAt": None,
            "heartbeatAt": None,
            "lastProgressAt": None,
            "execUnits": [],
            "summary": None
        })

    @classmethod
    def upsert_exec_unit(
        cls,
        task_id: str,
        unit_id: str,
        kind: str = "batch",
        index: Optional[int] = None,
        scope: Optional[str] = None,
        service: Optional[str] = None,
        state: Optional[str] = None,
        action: Optional[str] = None,
        request_started: bool = False,
        attempt: Optional[int] = None,
        retry_reason: Optional[str] = None,
        retry_wait_seconds: Optional[float] = None,
        error: Optional[str] = None,
        reset_retry: bool = False,
        force: bool = False
    ):
        """Creates or updates one per-execution-unit status and flushes progress detail.

        Each concurrent batch/page owns its own unit entry, so unit 5 can never
        overwrite the waiting information of unit 3.
        """
        detail = cls._get_detail(task_id)
        units = detail.setdefault("execUnits", [])
        unit = next((u for u in units if u.get("id") == unit_id), None)
        if unit is None:
            unit = {
                "id": unit_id,
                "kind": kind,
                "index": index,
                "scope": scope,
                "service": service,
                "state": state or "queued",
                "action": action,
                "requestStartedAt": None,
                "attempt": attempt or 1,
                "retryReason": None,
                "retryWaitSeconds": None,
                "error": None,
                "startedAt": utc_now_iso(),
                "finishedAt": None
            }
            units.append(unit)
        if index is not None:
            unit["index"] = index
        if kind:
            unit["kind"] = kind
        if scope is not None:
            unit["scope"] = scope
        if service is not None:
            unit["service"] = service
        if state is not None:
            unit["state"] = state
        if action is not None:
            unit["action"] = action
        if request_started:
            unit["requestStartedAt"] = utc_now_iso()
        if attempt is not None:
            unit["attempt"] = attempt
        if error is not None:
            unit["error"] = cls.sanitize_error(error)
        if reset_retry:
            unit["retryReason"] = None
            unit["retryWaitSeconds"] = None
        else:
            if retry_reason is not None:
                unit["retryReason"] = cls.sanitize_error(retry_reason)
            if retry_wait_seconds is not None:
                unit["retryWaitSeconds"] = retry_wait_seconds
        cls.update_progress_detail(task_id, force=force)

    @classmethod
    def finish_exec_unit(
        cls,
        task_id: str,
        unit_id: str,
        state: str = "succeeded",
        error: Optional[str] = None,
        force: bool = False
    ):
        """Marks a unit terminal, clears its request timer and prunes old finished units."""
        detail = cls._get_detail(task_id)
        units = detail.setdefault("execUnits", [])
        unit = next((u for u in units if u.get("id") == unit_id), None)
        if unit is not None:
            unit["state"] = state if state in TERMINAL_UNIT_STATES else "succeeded"
            unit["finishedAt"] = utc_now_iso()
            unit["requestStartedAt"] = None
            unit["retryReason"] = None
            unit["retryWaitSeconds"] = None
            if error is not None:
                unit["error"] = cls.sanitize_error(error)
            finished = [u for u in units if u.get("finishedAt")]
            if len(finished) > MAX_FINISHED_UNITS:
                finished_sorted = sorted(finished, key=lambda u: u.get("finishedAt") or "")
                for old in finished_sorted[:len(finished_sorted) - MAX_FINISHED_UNITS]:
                    if old.get("id") != unit_id:
                        units.remove(old)
        cls.update_progress_detail(task_id, force=force)

    @classmethod
    def finish_all_active_units(cls, task_id: str, state: str = "cancelled"):
        detail = cls._get_detail(task_id)
        for unit in detail.get("execUnits", []):
            if unit.get("state") in ACTIVE_UNIT_STATES:
                unit["state"] = state if state in TERMINAL_UNIT_STATES else "cancelled"
                unit["finishedAt"] = utc_now_iso()
                unit["requestStartedAt"] = None

    @classmethod
    def make_unit_event_handler(
        cls,
        task_id: str,
        unit_id: str,
        label: str,
        stage: Optional[str] = None,
        unit_index: Optional[int] = None,
        service: Optional[str] = None
    ) -> Callable[[str, Dict[str, Any]], Any]:
        """Builds a unified on_event callback mapping provider-call lifecycle events
        (slot_wait / request_start / response_received / retry_wait) onto one exec unit."""
        service_label = SERVICE_LABELS.get(service or "", "外部服务")

        async def handler(event: str, info: Optional[Dict[str, Any]] = None):
            info = info or {}
            if event == "slot_wait":
                waited = float(info.get("waited_seconds") or 0.0)
                cls.upsert_exec_unit(
                    task_id, unit_id, service=service, state="waiting_slot",
                    action=f"等待{service_label}调用名额"
                )
                if waited >= 0.5:
                    cls.log_event(
                        task_id, "slot_wait",
                        f"{label}等待调用名额 {waited:.1f} 秒",
                        stage=stage, unit_index=unit_index
                    )
            elif event == "request_start":
                cls.upsert_exec_unit(
                    task_id, unit_id, service=service, state="awaiting_response",
                    action=f"等待{service_label}响应",
                    request_started=True,
                    attempt=info.get("attempt"),
                    reset_retry=True
                )
                detail_now = cls._task_details.get(task_id) or {}
                unit_now = next(
                    (u for u in detail_now.get("execUnits", []) if u.get("id") == unit_id),
                    {}
                )
                scope_txt = info.get("scope") or unit_now.get("scope") or "当前批次"
                cls.log_event(
                    task_id, "request_start",
                    f"{label}请求已发送（范围：{scope_txt}）",
                    stage=stage, unit_index=unit_index
                )
            elif event == "response_received":
                cls.upsert_exec_unit(
                    task_id, unit_id, state="processing", action="正在校验响应...",
                    reset_retry=True
                )
                cls.log_event(
                    task_id, "response_received",
                    f"{label}收到模型响应",
                    stage=stage, unit_index=unit_index
                )
            elif event == "retry_wait":
                attempt = info.get("attempt")
                wait_s = info.get("wait_seconds")
                reason = info.get("reason")
                cls.upsert_exec_unit(
                    task_id, unit_id, state="retry_wait", attempt=attempt,
                    retry_reason=reason, retry_wait_seconds=wait_s,
                    action=f"重试等待（第 {attempt} 次尝试）"
                )
                cls.log_event(
                    task_id, "retry_wait",
                    f"{label}重试等待（第 {attempt} 次尝试）: {reason}",
                    stage=stage, unit_index=unit_index
                )
                try:
                    cls.record_timing(task_id, "retry_wait", float(wait_s or 0.0) * 1000.0)
                except (TypeError, ValueError):
                    pass

        return handler

    @classmethod
    def _summarize_units(cls, detail: Dict[str, Any]) -> Dict[str, Any]:
        units = detail.get("execUnits") or []
        active = [u for u in units if u.get("state") in ACTIVE_UNIT_STATES]

        def wait_key(u: Dict[str, Any]) -> str:
            return u.get("requestStartedAt") or u.get("startedAt") or ""

        longest = None
        for u in active:
            if u.get("state") in ("awaiting_response", "waiting_slot", "retry_wait"):
                if longest is None or wait_key(u) < wait_key(longest):
                    longest = u

        state_reasons = {
            "waiting_slot": "等待调用名额",
            "retry_wait": "重试等待",
        }
        if longest is None:
            waiting_reason = None
            waiting_since = None
        else:
            if longest.get("state") == "awaiting_response":
                waiting_reason = f"等待{SERVICE_LABELS.get(longest.get('service') or '', '服务')}响应"
            else:
                waiting_reason = state_reasons.get(longest.get("state"), longest.get("state"))
            waiting_since = longest.get("requestStartedAt") or longest.get("startedAt")

        return {
            "total": detail.get("total"),
            "succeeded": detail.get("succeeded", 0),
            "failed": detail.get("failed", 0),
            "skipped": detail.get("skipped", 0),
            "activeCount": len(active),
            "retryingCount": len([u for u in active if u.get("state") == "retry_wait"]),
            "waitingReason": waiting_reason,
            "waitingSince": waiting_since
        }

    @classmethod
    def update_progress_detail(cls, task_id: str, force: bool = False, **kwargs):
        """
        Updates task progress detail with throttling (max once per second to SQLite).
        Maintains real progress metrics and timestamps. When per-unit statuses exist,
        the task-level legacy fields (activeUnits / currentScope / currentAction /
        requestStartedAt / attempt / phase) are aggregated from them unless explicitly
        passed, so concurrent units never overwrite each other's waiting info.
        """
        detail = cls._get_detail(task_id)
        old_succeeded = detail.get("succeeded", 0)
        old_results = detail.get("resultCount", 0)
        detail.update(kwargs)

        units = detail.setdefault("execUnits", [])
        active = [u for u in units if u.get("state") in ACTIVE_UNIT_STATES]
        passed = set(kwargs.keys())

        if units:
            if "activeUnits" not in passed:
                detail["activeUnits"] = sorted({
                    u["index"] for u in active if u.get("index") is not None
                })
            if "attempt" not in passed:
                attempts = [u.get("attempt", 1) for u in active] or [detail.get("attempt", 1)]
                detail["attempt"] = max(attempts)
            if "requestStartedAt" not in passed:
                waiting = [
                    u for u in active
                    if u.get("state") == "awaiting_response" and u.get("requestStartedAt")
                ]
                detail["requestStartedAt"] = (
                    min(waiting, key=lambda u: u["requestStartedAt"])["requestStartedAt"]
                    if waiting else None
                )
            if "currentScope" not in passed:
                focus = next(
                    (u for u in active if u.get("state") == "awaiting_response"),
                    None
                ) or next((u for u in active if u.get("scope")), None)
                detail["currentScope"] = focus.get("scope") if focus else None
            if "currentAction" not in passed:
                focus = next(
                    (u for u in active if u.get("state") == "awaiting_response"),
                    None
                ) or next((u for u in active if u.get("action")), None)
                detail["currentAction"] = focus.get("action") if focus else None
            if "phase" not in passed:
                if any(u.get("state") == "retry_wait" for u in active):
                    detail["phase"] = "retry_wait"
                elif any(u.get("state") in ("waiting_slot", "awaiting_response") for u in active):
                    detail["phase"] = "waiting_provider"
                else:
                    detail["phase"] = "working"
            detail["summary"] = cls._summarize_units(detail)

        now_iso = utc_now_iso()
        if detail.get("succeeded", 0) != old_succeeded or detail.get("resultCount", 0) != old_results:
            detail["lastProgressAt"] = now_iso

        total = detail.get("total")
        succeeded = detail.get("succeeded", 0)
        pct = None
        # Tasks driving their own explicit progress (e.g. parse_document) must
        # never have tasks.progress overwritten by the derived succeeded/total
        # ratio — unit/heartbeat flushes would otherwise bounce it to 0 or 99.
        if detail.get("progressMode") != "explicit" and total is not None and total > 0:
            pct = min(99, int((succeeded / total) * 100))

        now_mono = time.monotonic()
        last_flush = cls._last_db_flush.get(task_id, 0.0)
        if force or (now_mono - last_flush >= 1.0):
            cls._last_db_flush[task_id] = now_mono
            timings = cls._task_timings.get(task_id)
            msg = detail.get("currentAction") or None
            try:
                with db_session() as conn:
                    if pct is not None:
                        conn.execute("""
                            UPDATE tasks
                            SET progress = ?,
                                message = COALESCE(?, message),
                                progress_detail_json = ?,
                                timing_breakdown_json = ?,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = ? AND status IN ('pending', 'processing', 'cancel_requested')
                        """, (
                            pct, msg,
                            json.dumps(detail, ensure_ascii=False),
                            json.dumps(timings, ensure_ascii=False) if timings else None,
                            task_id
                        ))
                    else:
                        conn.execute("""
                            UPDATE tasks
                            SET message = COALESCE(?, message),
                                progress_detail_json = ?,
                                timing_breakdown_json = ?,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = ? AND status IN ('pending', 'processing', 'cancel_requested')
                        """, (
                            msg,
                            json.dumps(detail, ensure_ascii=False),
                            json.dumps(timings, ensure_ascii=False) if timings else None,
                            task_id
                        ))
            except Exception as exc:
                logger.warning("Error flushing progress to DB for %s: %s", task_id, exc)

    @classmethod
    async def _heartbeat_loop(cls):
        """Sends periodic heartbeats every 5 seconds for running tasks."""
        while cls._running:
            try:
                await asyncio.sleep(5.0)
                running_ids = list(cls._running_tasks.keys())
                now_iso = utc_now_iso()
                for t_id in running_ids:
                    if t_id in cls._task_details:
                        cls._task_details[t_id]["heartbeatAt"] = now_iso
                        cls.update_progress_detail(t_id, force=True)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Error in heartbeat loop: %s", exc)

    @classmethod
    def save_checkpoint(
        cls,
        task_type: str,
        target_id: str,
        unit_key: str,
        unit_index: int,
        status: str,
        result_count: int = 0,
        data: Optional[Dict[str, Any]] = None,
        error_msg: Optional[str] = None,
        model_version: Optional[str] = None,
        conn: Optional[Any] = None,
    ):
        """Persists processed unit checkpoint to allow selective resumption on retry."""
        try:
            def _save(active_conn: Any) -> None:
                active_conn.execute("""
                    INSERT INTO task_checkpoints (
                        id, task_type, target_id, unit_key, unit_index, status,
                        result_count, data_json, error_msg, model_version, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(task_type, target_id, unit_key) DO UPDATE SET
                        status = excluded.status,
                        result_count = excluded.result_count,
                        data_json = excluded.data_json,
                        error_msg = excluded.error_msg,
                        model_version = excluded.model_version,
                        updated_at = CURRENT_TIMESTAMP
                """, (
                    str(uuid.uuid4()), task_type, target_id, unit_key, unit_index,
                    status, result_count, json.dumps(data, ensure_ascii=False) if data else None,
                    error_msg, model_version
                ))
            if conn is not None:
                _save(conn)
            else:
                with db_session() as active_conn:
                    _save(active_conn)
        except Exception as exc:
            logger.warning("Failed to save checkpoint for %s/%s/%s: %s", task_type, target_id, unit_key, exc)

    @classmethod
    def get_completed_checkpoints(cls, task_type: str, target_id: str) -> Dict[str, Dict[str, Any]]:
        """Returns completed checkpoints for a given task target."""
        try:
            with db_session() as conn:
                rows = conn.execute("""
                    SELECT unit_key, unit_index, status, result_count, data_json, error_msg, model_version
                    FROM task_checkpoints
                    WHERE task_type = ? AND target_id = ? AND status = 'completed'
                """, (task_type, target_id)).fetchall()
                return {r["unit_key"]: dict(r) for r in rows}
        except Exception as exc:
            logger.warning("Failed to load checkpoints for %s/%s: %s", task_type, target_id, exc)
            return {}

    @classmethod
    def get_completed_checkpoints_for_snapshot(
        cls, task_type: str, target_id: str, source_snapshot_hash: Optional[str]
    ) -> Dict[str, Dict[str, Any]]:
        """Load completed checkpoints only when their source snapshot matches.

        Older checkpoints have no snapshot metadata and are deliberately not
        reused when the caller supplies a source hash.  This prevents a retry
        from treating stale extraction output as valid after source edits.
        """
        checkpoints = cls.get_completed_checkpoints(task_type, target_id)
        if not source_snapshot_hash:
            return checkpoints
        matched: Dict[str, Dict[str, Any]] = {}
        for key, checkpoint in checkpoints.items():
            raw = checkpoint.get("data_json")
            try:
                data = json.loads(raw) if raw else {}
            except (TypeError, ValueError):
                data = {}
            if data.get("source_snapshot_hash") == source_snapshot_hash:
                matched[key] = checkpoint
        return matched

    @classmethod
    def sync_extraction_batch_for_task(
        cls, task_id: str, task_status: str, message: Optional[str] = None
    ) -> bool:
        """Synchronize an extraction batch owned by a task.

        The conditional ownership check is the generation guard: an old task
        finishing late cannot overwrite a batch already retried onto a newer
        task.  The helper is best effort for legacy databases without the
        extraction table and intentionally does not create schema columns.
        """
        status_map = {
            "completed": "completed",
            "partial": "partial",
            "failed": "failed",
            "cancelled": "cancelled",
        }
        batch_status = status_map.get(task_status)
        if not batch_status:
            return False
        try:
            with db_session() as conn:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(extraction_batches)").fetchall()}
                if not columns or "task_id" not in columns:
                    return False
                assignments = ["status = ?", "updated_at = CURRENT_TIMESTAMP"]
                params: list[Any] = [batch_status]
                if message and "error_msg" in columns and batch_status in {"failed", "partial", "cancelled"}:
                    assignments.append("error_msg = ?")
                    params.append(message)
                params.append(task_id)
                cur = conn.execute(
                    f"UPDATE extraction_batches SET {', '.join(assignments)} "
                    "WHERE task_id = ? AND status IN ('processing', 'pending')",
                    params,
                )
                return cur.rowcount > 0
        except Exception:
            logger.exception("Failed to synchronize extraction batch for task %s", task_id)
            return False

    @classmethod
    def is_cancelled(cls, task_id: str) -> bool:
        with db_session() as conn:
            row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return row is not None and row["status"] in ("cancel_requested", "cancelled")

    @classmethod
    def finalize_cancellation(cls, task_id: str, message: str = "任务已取消"):
        cls.log_event(task_id, "cancelled", message)
        cls.finish_all_active_units(task_id, state="cancelled")
        cls.update_progress_detail(task_id, phase="working", currentAction=message, force=True)
        elapsed_ms = None
        if task_id in cls._task_start_mono:
            elapsed_ms = round((time.monotonic() - cls._task_start_mono[task_id]) * 1000.0, 2)
        log_diagnostic("task_cancelled", level=logging.INFO, message=message, task_id=task_id, duration_ms=elapsed_ms)
        with db_session() as conn:
            conn.execute(
                "UPDATE tasks SET status = 'cancelled', message = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND status IN ('cancel_requested', 'processing', 'pending')",
                (message, task_id)
            )

    @classmethod
    def request_cancel(cls, task_id: str) -> Optional[Dict[str, Any]]:
        with db_session() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not row:
                return None
            status = row["status"]
            if status == "pending":
                conn.execute(
                    "UPDATE tasks SET status = 'cancelled', message = '已取消', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (task_id,)
                )
                cls.log_event(task_id, "cancelled", "已在等待队列中取消任务", conn=conn)
            elif status == "processing":
                conn.execute(
                    "UPDATE tasks SET status = 'cancel_requested', message = '正在停止任务...', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (task_id,)
                )
                cls.log_event(task_id, "cancel_requested", "已请求取消任务，等待当前步骤终止", conn=conn)
            elif status in ("completed", "partial", "failed", "cancelled", "cancel_requested"):
                return dict(row)

            if task_id in cls._running_tasks:
                rt = cls._running_tasks[task_id]
                if not rt.done():
                    rt.cancel()

            updated = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return dict(updated)

    @classmethod
    def update_task_progress(cls, task_id: str, progress: int, message: str, status: str = "processing"):
        with db_session() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            UPDATE tasks 
            SET progress = ?, message = ?, status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status IN ('pending', 'processing')
            """, (progress, message, status, task_id))

    @classmethod
    def mark_task_failed(cls, task_id: str, error_msg: str):
        safe_msg = cls.sanitize_error(error_msg)
        cls.log_event(task_id, "failed", f"任务执行失败: {safe_msg}")
        cls.update_progress_detail(task_id, phase="working", currentAction="执行失败", force=True)
        timings = cls._task_timings.get(task_id)
        detail = cls._task_details.get(task_id)
        elapsed_ms = None
        if task_id in cls._task_start_mono:
            elapsed_ms = round((time.monotonic() - cls._task_start_mono[task_id]) * 1000.0, 2)
        log_diagnostic("task_failed", level=logging.ERROR, message=f"任务执行失败: {safe_msg}", task_id=task_id, duration_ms=elapsed_ms)
        with db_session() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            UPDATE tasks 
            SET status = 'failed', error_msg = ?, message = '执行失败',
                progress_detail_json = COALESCE(?, progress_detail_json),
                timing_breakdown_json = COALESCE(?, timing_breakdown_json),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status IN ('processing', 'pending', 'cancel_requested')
            """, (
                error_msg,
                json.dumps(detail, ensure_ascii=False) if detail else None,
                json.dumps(timings, ensure_ascii=False) if timings else None,
                task_id
            ))

    @classmethod
    def mark_task_partial(cls, task_id: str, error_msg: str):
        safe_msg = cls.sanitize_error(error_msg)
        cls.log_event(task_id, "partial", f"部分批次执行失败: {safe_msg}")
        cls.update_progress_detail(task_id, phase="working", currentAction="部分批次执行失败", force=True)
        timings = cls._task_timings.get(task_id)
        detail = cls._task_details.get(task_id)
        elapsed_ms = None
        if task_id in cls._task_start_mono:
            elapsed_ms = round((time.monotonic() - cls._task_start_mono[task_id]) * 1000.0, 2)
        log_diagnostic("task_partial", level=logging.WARNING, message=f"部分批次执行失败: {safe_msg}", task_id=task_id, duration_ms=elapsed_ms)
        with db_session() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            UPDATE tasks
            SET status = 'partial', error_msg = ?, message = '部分批次执行失败',
                progress_detail_json = COALESCE(?, progress_detail_json),
                timing_breakdown_json = COALESCE(?, timing_breakdown_json),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status IN ('processing', 'pending', 'cancel_requested')
            """, (
                error_msg,
                json.dumps(detail, ensure_ascii=False) if detail else None,
                json.dumps(timings, ensure_ascii=False) if timings else None,
                task_id
            ))

    @classmethod
    def mark_task_completed(cls, task_id: str, message: str = "执行完成", conn: Optional[Any] = None) -> bool:
        cls.log_event(task_id, "completed", message, conn=conn)
        if conn is None:
            cls.update_progress_detail(task_id, phase="working", currentAction=message, force=True)
        else:
            # A caller saving its result in the same transaction must not open
            # a second writer while holding SQLite's write lock.
            cls._get_detail(task_id).update(phase="working", currentAction=message)
        timings = cls._task_timings.get(task_id)
        detail = cls._task_details.get(task_id)
        elapsed_ms = None
        if task_id in cls._task_start_mono:
            elapsed_ms = round((time.monotonic() - cls._task_start_mono[task_id]) * 1000.0, 2)
        res_cnt = (detail.get("resultCount") if detail else None) or (detail.get("succeeded", 0) if detail else 0)
        log_diagnostic("task_completed", level=logging.INFO, message=message, task_id=task_id, duration_ms=elapsed_ms, result_count=res_cnt)
        with (nullcontext(conn) if conn is not None else db_session()) as active_conn:
            cursor = active_conn.cursor()
            cursor.execute("""
            UPDATE tasks 
            SET status = 'completed', progress = 100, message = ?,
                progress_detail_json = COALESCE(?, progress_detail_json),
                timing_breakdown_json = COALESCE(?, timing_breakdown_json),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status IN ('pending', 'processing')
            """, (
                message,
                json.dumps(detail, ensure_ascii=False) if detail else None,
                json.dumps(timings, ensure_ascii=False) if timings else None,
                task_id
            ))
            return cursor.rowcount > 0

    @classmethod
    async def _run_loop(cls):
        # Reset any leftover 'processing' or 'cancel_requested' tasks on startup to 'cancelled'
        with db_session() as conn:
            cursor = conn.cursor()
            stale_rows = cursor.execute(
                "SELECT id FROM tasks WHERE status IN ('processing', 'cancel_requested')"
            ).fetchall()
            cursor.execute("""
                UPDATE tasks
                SET status = 'cancelled', message = '服务重启，任务已取消', updated_at = CURRENT_TIMESTAMP
                WHERE status IN ('processing', 'cancel_requested')
            """)
        for stale in stale_rows:
            cls.sync_extraction_batch_for_task(stale["id"], "cancelled", "服务重启，任务已取消")

        while cls._running:
            try:
                task = None
                with db_session() as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                    SELECT id, task_type, target_id 
                    FROM tasks 
                    WHERE status = 'pending' 
                    ORDER BY created_at ASC 
                    LIMIT 1
                    """)
                    row = cursor.fetchone()
                    if row:
                        task = dict(row)
                        cursor.execute("UPDATE tasks SET status = 'processing', progress = 5, message = '开始处理', started_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'pending'", (task["id"],))
                        if cursor.rowcount == 0:
                            # State changed concurrently (e.g. cancelled while pending)
                            task = None
                        else:
                            cls._task_start_mono[task["id"]] = time.monotonic()

                if task:
                    await cls._dispatch_task(task)
                else:
                    await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in TaskWorker loop: %s", str(e), exc_info=True)
                await asyncio.sleep(2.0)

    @classmethod
    async def _dispatch_task(cls, task: Dict[str, Any]):
        task_id = task["id"]
        if cls.is_cancelled(task_id):
            cls.finalize_cancellation(task_id)
            return

        task_type = task["task_type"]
        target_id = task["target_id"]

        with task_context(task_id, task_type):
            log_diagnostic(
                "task_started",
                level=logging.INFO,
                message=f"任务开始执行 (类型: {task_type})",
                task_id=task_id,
                task_type=task_type,
                target_id=target_id
            )
            exec_task = asyncio.create_task(cls._route_task_execution(task_id, task_type, target_id))
            cls._running_tasks[task_id] = exec_task

            try:
                await exec_task
            except asyncio.CancelledError:
                logger.info("Task %s received asyncio cancellation", task_id)
                cls.finalize_cancellation(task_id)
                cls.sync_extraction_batch_for_task(task_id, "cancelled", "任务已取消")
            except Exception as e:
                safe_msg = cls.sanitize_error(e)
                logger.error("Task %s failed: %s", task_id, safe_msg)
                if not cls.is_cancelled(task_id):
                    cls.mark_task_failed(task_id, str(e))
                    cls.sync_extraction_batch_for_task(task_id, "failed", safe_msg)
                    if task_type == "parse_document" and target_id:
                        with db_session() as conn:
                            conn.execute(
                                "UPDATE documents SET status = 'failed', error_msg = ? WHERE id = ?",
                                (safe_msg, target_id),
                            )
                else:
                    cls.finalize_cancellation(task_id)
                    cls.sync_extraction_batch_for_task(task_id, "cancelled", "任务已取消")
            finally:
                try:
                    with db_session() as conn:
                        final_row = conn.execute("SELECT status, error_msg, message FROM tasks WHERE id = ?", (task_id,)).fetchone()
                    if final_row and final_row["status"] in ("completed", "partial", "failed", "cancelled"):
                        cls.sync_extraction_batch_for_task(
                            task_id, final_row["status"], final_row["error_msg"] or final_row["message"]
                        )
                except Exception:
                    logger.exception("Failed final extraction batch synchronization for task %s", task_id)
                cls._running_tasks.pop(task_id, None)

    @classmethod
    async def _route_task_execution(cls, task_id: str, task_type: str, target_id: str):
        if task_type == "parse_document":
            await cls._execute_parse_document(task_id, target_id)
        elif task_type == "extract_questions":
            await cls._execute_extract_questions(task_id, target_id)
        elif task_type == "generate_card":
            await cls._execute_generate_card(task_id, target_id)
        elif task_type == "rebuild_vector_index":
            from app.desktop import execute_reindex_task
            await execute_reindex_task(task_id, target_id)
        elif task_type == "reindex_chunk":
            await cls._execute_reindex_chunk(task_id, target_id)
        elif task_type == "extract_knowledge_points":
            from app.services.knowledge_extractor import KnowledgeExtractor
            await KnowledgeExtractor.execute_extraction_task(task_id, target_id)
        elif task_type == "generate_recall_cards":
            from app.services.recall_generator import RecallGenerator
            await RecallGenerator.execute_generation_task(task_id, target_id)
        elif task_type == "generate_library_guide":
            await cls._execute_library_guide(task_id, target_id)
        else:
            cls.mark_task_failed(task_id, f"未知任务类型: {task_type}")

    @classmethod
    async def _execute_library_guide(cls, task_id: str, request_id: str):
        from app.models.library import GuideRequest
        from app.services import library

        cls.update_task_progress(task_id, 10, "正在读取教材、管理学资料和真题")
        cls.update_progress_detail(task_id, stage="read_sources", progressMode="explicit",
                                   currentAction="正在读取章节资料", force=True)
        cls.log_event(task_id, "reading_sources", "正在读取并核验章节资料范围", stage="read_sources")
        try:
            with db_session() as conn:
                row = conn.execute("SELECT * FROM library_guide_requests WHERE id=?", (request_id,)).fetchone()
            if not row:
                raise RuntimeError("missing persisted guide request")
            req = GuideRequest.model_validate_json(row["request_json"])
            with unit_context(unit_id="guide-model", scope=req.chapter_id):
                await library.generate_guide(req, task_id=task_id,
                                             expected_snapshot=json.loads(row["source_snapshot_json"]))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if cls.is_cancelled(task_id):
                raise asyncio.CancelledError() from exc
            # Exception strings can contain request bodies/provider secrets.
            # Report only safe typed causes and application-authored messages.
            safe = library.guide_task_error(exc)
            cls.finish_exec_unit(task_id, "guide-model", state="failed", error=safe, force=True)
            cls.update_progress_detail(task_id, failed=1, force=True)
            cls.mark_task_failed(task_id, safe)

    @classmethod
    async def _execute_reindex_chunk(cls, task_id: str, chunk_id: str):
        cls.update_task_progress(task_id, 20, "正在更新校对切片的语义向量...")
        with db_session() as conn:
            row = conn.execute(
                "SELECT id, content, parse_error FROM document_chunks WHERE id = ?",
                (chunk_id,),
            ).fetchone()
        if not row:
            cls.mark_task_failed(task_id, "原文切片不存在")
            return
        if row["parse_error"] or not row["content"].strip():
            cls.mark_task_failed(task_id, "原文切片仍为空或存在解析错误")
            return

        from app.database import is_vector_locked
        locked, lock_reason = is_vector_locked()
        if locked:
            cls.mark_task_failed(task_id, f"向量索引已锁定，禁止追加向量: {lock_reason}")
            return

        source = current_embedding_source()
        try:
            vector = await EmbeddingService.get_embedding(row["content"])
            blob = EmbeddingService.serialize_embedding(vector)
        except Exception as exc:
            cls.mark_task_failed(task_id, f"语义向量更新失败: {type(exc).__name__}")
            return
        content_changed = False
        try:
            with db_session() as conn:
                assert_embedding_source(conn, source)
                updated = conn.execute(
                    "UPDATE document_chunks SET embedding = ?, embedding_status = 'completed', updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ? AND content = ? AND parse_error IS NULL",
                    (blob, chunk_id, row["content"]),
                ).rowcount
                if updated:
                    record_embedding_source(conn, source)
                else:
                    content_changed = True
        except Exception as exc:
            cls.mark_task_failed(task_id, f"语义向量写入失败: {exc}")
            return
        if content_changed:
            cls.mark_task_completed(task_id, "切片内容在向量化期间发生变化，已保留新的待处理索引")
            return
        with db_session() as conn:
            doc_row = conn.execute(
                "SELECT doc_id FROM document_chunks WHERE id = ?", (chunk_id,)
            ).fetchone()
            if doc_row:
                incomplete = conn.execute(
                    "SELECT COUNT(*) AS count FROM document_chunks "
                    "WHERE doc_id = ? AND (parse_error IS NOT NULL OR content = '' OR embedding_status <> 'completed')",
                    (doc_row["doc_id"],),
                ).fetchone()["count"]
                if incomplete == 0:
                    conn.execute(
                        "UPDATE documents SET status = 'completed', error_msg = NULL WHERE id = ?",
                        (doc_row["doc_id"],),
                    )
        cls.mark_task_completed(task_id, "原文切片索引与语义向量已更新")

    @classmethod
    async def _execute_parse_document(cls, task_id: str, doc_id: str):
        t0 = time.monotonic()
        cls.log_event(task_id, "stage_change", "开始读取文档信息", stage="read_doc")
        cls.update_progress_detail(
            task_id,
            stage="read_doc",
            phase="working",
            unit="page",
            total=None,
            succeeded=0,
            failed=0,
            skipped=0,
            currentAction="读取文档信息...",
            progressMode="explicit",
            force=True
        )
        cls.update_task_progress(task_id, 10, "读取文档信息...")

        doc_info = None
        with db_session() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, filename, file_hash, file_type, year, session_name FROM documents WHERE id = ?", (doc_id,))
            row = cursor.fetchone()
            if row:
                doc_info = dict(row)

        if not doc_info:
            cls.mark_task_failed(task_id, f"文档不存在: {doc_id}")
            return

        from app.config import settings
        # Find file in uploads_path with matching hash
        file_path = None
        for ext in [".pdf", ".docx", ".jpg", ".jpeg", ".png"]:
            candidate = settings.uploads_path / f"{doc_info['file_hash']}{ext}"
            if candidate.exists():
                file_path = candidate
                break

        if not file_path:
            cls.mark_task_failed(task_id, "原始文件在存储中缺失")
            return

        suffix = file_path.suffix.lower()
        cls.record_timing(task_id, "local_parse", (time.monotonic() - t0) * 1000.0)

        t_parse = time.monotonic()
        cls.log_event(task_id, "stage_change", f"正在解析 {suffix} 文件内容", stage="parse_file")
        cls.update_progress_detail(
            task_id,
            stage="parse_file",
            phase="working",
            unit="page",
            total=None,
            currentAction=f"正在解析 {suffix} 文件内容...",
            force=True
        )
        cls.update_task_progress(task_id, 20, f"正在解析 {suffix} 文件内容...")

        chunks = []
        source_page_count = 0
        def _parse_progress(p, m):
            if cls.is_cancelled(task_id):
                raise asyncio.CancelledError("任务已请求取消")
            cls.update_task_progress(task_id, 20 + int(p * 0.4), m)
            cls.update_progress_detail(task_id, currentAction=m)

        def _on_ocr_timing(dur_ms):
            cls.record_timing(task_id, "ocr_request", dur_ms)

        def _make_ocr_unit_handler(page_num: Optional[int]):
            """Creates a per-page OCR exec unit and returns its unified event handler."""
            unit_id = f"page-{page_num if page_num is not None else 'image'}"
            scope = f"第 {page_num} 页" if page_num is not None else "整图"
            label = f"第 {page_num} 页" if page_num is not None else "图片"
            cls.upsert_exec_unit(
                task_id, unit_id,
                kind="page", index=page_num, scope=scope, service="ocr",
                state="waiting_slot", action="等待OCR调用名额", force=True
            )
            return cls.make_unit_event_handler(
                task_id, unit_id, label=label,
                stage="parse_file", unit_index=page_num, service="ocr"
            )

        def _finish_ocr_unit(page_num: Optional[int], ok: bool, error: Optional[str]):
            unit_id = f"page-{page_num if page_num is not None else 'image'}"
            cls.finish_exec_unit(
                task_id, unit_id,
                state="succeeded" if ok else "failed",
                error=error, force=True
            )

        if suffix == ".pdf":
            import pymupdf as fitz
            with fitz.open(file_path) as source_document:
                source_page_count = len(source_document)
            cls.update_progress_detail(task_id, total=source_page_count)
            chunks = await PDFParser.parse_pdf(
                file_path,
                progress_callback=_parse_progress,
                on_ocr_timing=_on_ocr_timing,
                on_ocr_unit_start=_make_ocr_unit_handler,
                on_ocr_unit_finish=_finish_ocr_unit
            )
        elif suffix in [".docx", ".doc"]:
            chunks = DocxParser.parse_docx(
                file_path,
                progress_callback=_parse_progress
            )
        elif suffix in [".jpg", ".jpeg", ".png"]:
            chunks = await ImageParser.parse_image(
                file_path,
                progress_callback=_parse_progress,
                on_ocr_event=_make_ocr_unit_handler(None),
                on_ocr_finish=lambda ok, err: _finish_ocr_unit(None, ok, err)
            )
        else:
            cls.mark_task_failed(task_id, f"不支持的文件后缀: {suffix}")
            return

        cls.record_timing(task_id, "local_parse", (time.monotonic() - t_parse) * 1000.0)

        if cls.is_cancelled(task_id):
            cls.finalize_cancellation(task_id)
            return

        if not chunks:
            cls.mark_task_failed(task_id, "文档未提取到可用内容")
            with db_session() as conn:
                conn.execute(
                    "UPDATE documents SET status = 'failed', error_msg = ? WHERE id = ?",
                    ("文档未提取到可用内容", doc_id),
                )
            return

        parse_errors = []
        t_save = time.monotonic()
        cls.log_event(task_id, "stage_change", f"完成文本提取，共 {len(chunks)} 个片段，保存文本与全文索引", stage="save_chunks_fts")
        cls.update_task_progress(task_id, 65, f"完成文本提取，共 {len(chunks)} 个片段，正在建立索引与向量化...")
        cls.update_progress_detail(
            task_id,
            stage="save_chunks_fts",
            phase="working",
            unit="chunk",
            total=len(chunks),
            succeeded=len(chunks),
            currentAction="正在写入文本切片与全文检索索引...",
            force=True
        )

        with db_session() as conn:
            cursor = conn.cursor()
            existing_rows = cursor.execute("""
                SELECT id, chunk_index, page_number, paragraph_ref, content,
                       original_content
                FROM document_chunks WHERE doc_id = ? ORDER BY chunk_index ASC
            """, (doc_id,)).fetchall()
            existing_by_key = {}
            existing_location_counts = {}
            for row in existing_rows:
                location = (row["page_number"], row["paragraph_ref"])
                local_index = existing_location_counts.get(location, 0)
                existing_location_counts[location] = local_index + 1
                existing_by_key[(location, local_index)] = row
            new_location_counts = {}
            reused_ids = []
            cursor.execute(
                "DELETE FROM document_chunks_fts WHERE chunk_id IN "
                "(SELECT id FROM document_chunks WHERE doc_id = ?)",
                (doc_id,),
            )

            for idx, chunk in enumerate(chunks):
                location = (chunk.get("page_number"), chunk.get("paragraph_ref"))
                local_index = new_location_counts.get(location, 0)
                new_location_counts[location] = local_index + 1
                key = (location, local_index)
                existing = existing_by_key.get(key)
                chunk_id = existing["id"] if existing else str(uuid.uuid4())
                parser_content = chunk.get("content", "")
                manually_corrected = bool(
                    existing
                    and existing["original_content"] is not None
                    and existing["content"] != existing["original_content"]
                    and existing["content"].strip()
                )
                content = existing["content"] if manually_corrected else parser_content
                parse_error = None if manually_corrected else chunk.get("parse_error")
                if parse_error:
                    parse_errors.append(parse_error)
                segmented = segment_chinese(content)
                reused_ids.append(chunk_id)
                cursor.execute("""
                    INSERT INTO document_chunks (
                        id, doc_id, chunk_index, page_number, heading, paragraph_ref,
                        content, content_segmented, original_content, embedding_status,
                        parse_error, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(id) DO UPDATE SET
                        doc_id = excluded.doc_id,
                        chunk_index = excluded.chunk_index,
                        page_number = excluded.page_number,
                        heading = excluded.heading,
                        paragraph_ref = excluded.paragraph_ref,
                        content = excluded.content,
                        content_segmented = excluded.content_segmented,
                        original_content = excluded.original_content,
                        embedding = NULL,
                        embedding_status = excluded.embedding_status,
                        parse_error = excluded.parse_error,
                        updated_at = CURRENT_TIMESTAMP
                """, (
                    chunk_id,
                    doc_id,
                    idx,
                    chunk.get("page_number"),
                    chunk.get("heading"),
                    chunk.get("paragraph_ref"),
                    content,
                    segmented,
                    parser_content,
                    "failed" if parse_error else "pending",
                    parse_error,
                ))
                if segmented and not parse_error:
                    cursor.execute(
                        "INSERT INTO document_chunks_fts (chunk_id, content_segmented) VALUES (?, ?)",
                        (chunk_id, segmented),
                    )

            manually_preserved = [
                row for row in existing_rows
                if row["id"] not in reused_ids
                and row["original_content"] is not None
                and row["content"] != row["original_content"]
                and row["content"].strip()
            ]
            for row in manually_preserved:
                reused_ids.append(row["id"])
                cursor.execute("""
                    UPDATE document_chunks
                    SET embedding = NULL, embedding_status = 'pending', parse_error = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (row["id"],))
                cursor.execute(
                    "INSERT INTO document_chunks_fts (chunk_id, content_segmented) VALUES (?, ?)",
                    (row["id"], segment_chinese(row["content"])),
                )

            stale_ids = [row["id"] for row in existing_rows if row["id"] not in reused_ids]
            if stale_ids:
                placeholders = ",".join("?" for _ in stale_ids)
                cursor.execute(
                    f"""
                    UPDATE questions
                    SET source_chunk_id = CASE WHEN source_chunk_id IN ({placeholders}) THEN NULL ELSE source_chunk_id END,
                        answer_source_chunk_id = CASE WHEN answer_source_chunk_id IN ({placeholders}) THEN NULL ELSE answer_source_chunk_id END,
                        is_confirmed = CASE WHEN source_chunk_id IN ({placeholders}) OR answer_source_chunk_id IN ({placeholders}) THEN 0 ELSE is_confirmed END,
                        needs_review = CASE WHEN source_chunk_id IN ({placeholders}) OR answer_source_chunk_id IN ({placeholders}) THEN 1 ELSE needs_review END,
                        review_reason = CASE WHEN source_chunk_id IN ({placeholders}) OR answer_source_chunk_id IN ({placeholders}) THEN '解析重试后来源切片变化，需重新核对' ELSE review_reason END
                    WHERE source_chunk_id IN ({placeholders}) OR answer_source_chunk_id IN ({placeholders})
                    """,
                    tuple(stale_ids) * 10,
                )
                cursor.execute(
                    f"DELETE FROM document_chunks WHERE id IN ({placeholders})",
                    tuple(stale_ids),
                )

            max_page = max(
                [c.get("page_number") or 0 for c in chunks],
                default=source_page_count,
            )
            max_page = max(max_page, source_page_count)
            if parse_errors:
                error_text = "；".join(dict.fromkeys(parse_errors))
                cursor.execute(
                    "UPDATE documents SET status = 'failed', error_msg = ?, page_count = ? WHERE id = ?",
                    (f"部分页面解析失败：{error_text}", max_page, doc_id),
                )
            else:
                cursor.execute(
                    "UPDATE documents SET status = 'processing', error_msg = NULL, page_count = ? WHERE id = ?",
                    (max_page, doc_id),
                )

        cls.record_timing(task_id, "db_write", (time.monotonic() - t_save) * 1000.0)

        with db_session() as conn:
            rows = conn.execute(
                "SELECT id, content FROM document_chunks "
                "WHERE doc_id = ? AND parse_error IS NULL AND content <> ''",
                (doc_id,),
            ).fetchall()

        t_emb = time.monotonic()
        cls.log_event(task_id, "stage_change", f"开始建立语义向量索引，共 {len(rows)} 个切片", stage="generate_embeddings")
        cls.update_progress_detail(
            task_id,
            stage="generate_embeddings",
            phase="working",
            unit="chunk",
            total=len(rows),
            succeeded=0,
            failed=0,
            currentAction="正在验证向量服务并计算语义向量...",
            force=True
        )
        cls.upsert_exec_unit(
            task_id, "embedding-batch",
            kind="chunk", index=1, scope=f"共 {len(rows)} 个切片", service="embedding",
            state="waiting_slot", action="等待向量服务调用名额", force=True
        )
        cls.update_task_progress(task_id, 80, "正在验证向量服务并计算语义向量...")

        # Compatibility probe: fail fast if remote embedding fails
        from app.database import is_vector_locked
        locked, lock_reason = is_vector_locked()
        if locked:
            cls.mark_task_failed(task_id, f"向量索引已锁定，禁止追加向量: {lock_reason}")
            with db_session() as conn:
                conn.execute(
                    "UPDATE documents SET status = 'failed', error_msg = ? WHERE id = ?",
                    (f"向量索引已锁定: {lock_reason}", doc_id),
                )
            return

        source = current_embedding_source()
        try:
            cls.upsert_exec_unit(
                task_id, "embedding-batch",
                state="awaiting_response", action="正在验证向量服务兼容性...",
                request_started=True, attempt=1, force=True
            )
            await EmbeddingService.verify_remote_compatibility()
        except Exception as exc:
            cls.record_timing(task_id, "embedding_request", (time.monotonic() - t_emb) * 1000.0)
            cls.log_event(task_id, "failed", f"向量服务不可用: {cls.sanitize_error(exc)}", stage="generate_embeddings")
            cls.finish_exec_unit(task_id, "embedding-batch", state="failed", error=str(exc), force=True)
            cls.mark_task_failed(task_id, f"向量服务不可用: {exc}")
            with db_session() as conn:
                conn.execute(
                    "UPDATE documents SET status = 'failed', error_msg = ? WHERE id = ?",
                    (f"向量服务不可用: {exc}", doc_id),
                )
            return

        if cls.is_cancelled(task_id):
            cls.finalize_cancellation(task_id)
            return

        embedding_errors = []
        chunk_contents = [r["content"] for r in rows]
        chunk_ids = [r["id"] for r in rows]

        try:
            # Record request start only when the batch embedding request is actually issued
            cls.upsert_exec_unit(
                task_id, "embedding-batch",
                state="awaiting_response", action="等待向量服务响应",
                request_started=True, attempt=1, force=True
            )
            vectors = await EmbeddingService.get_embeddings_batch(chunk_contents)
            if len(vectors) != len(chunk_contents):
                raise RuntimeError("向量服务返回条目数与切片数不一致，未写入索引")
            cls.record_timing(task_id, "embedding_request", (time.monotonic() - t_emb) * 1000.0)
            cls.upsert_exec_unit(
                task_id, "embedding-batch",
                state="saving", action="正在写入语义向量...", force=True
            )

            t_write_emb = time.monotonic()
            with db_session() as conn:
                assert_embedding_source(conn, source)
                written = 0
                for c_id, content, vec in zip(chunk_ids, chunk_contents, vectors):
                    blob = EmbeddingService.serialize_embedding(vec)
                    written += conn.execute(
                        "UPDATE document_chunks SET embedding = ?, embedding_status = 'completed', updated_at = CURRENT_TIMESTAMP "
                        "WHERE id = ? AND content = ? AND parse_error IS NULL",
                        (blob, c_id, content),
                    ).rowcount
                if written:
                    record_embedding_source(conn, source)
            cls.record_timing(task_id, "db_write", (time.monotonic() - t_write_emb) * 1000.0)
            cls.finish_exec_unit(task_id, "embedding-batch", state="succeeded", force=True)
            cls.update_progress_detail(
                task_id,
                phase="working",
                succeeded=len(rows),
                resultCount=len(rows),
                currentAction="语义向量建立完成",
                force=True
            )
        except Exception as exc:
            cls.record_timing(task_id, "embedding_request", (time.monotonic() - t_emb) * 1000.0)
            cls.finish_exec_unit(task_id, "embedding-batch", state="failed", error=str(exc), force=True)
            embedding_errors.append(f"切片批量向量化失败: {type(exc).__name__} - {exc}")
            with db_session() as conn:
                for c_id in chunk_ids:
                    conn.execute(
                        "UPDATE document_chunks SET embedding_status = 'failed', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (c_id,),
                    )

        if parse_errors or embedding_errors:
            errors = parse_errors + embedding_errors
            message = "；".join(dict.fromkeys(errors))
            if embedding_errors:
                message = f"向量化失败：{message}"
            cls.mark_task_failed(task_id, message)
            with db_session() as conn:
                conn.execute(
                    "UPDATE documents SET status = 'failed', error_msg = ? WHERE id = ?",
                    (message, doc_id),
                )
            return

        if cls.is_cancelled(task_id):
            cls.finalize_cancellation(task_id)
            with db_session() as conn:
                conn.execute(
                    "UPDATE documents SET status = 'pending', error_msg = '解析已取消，处理未完成' WHERE id = ?",
                    (doc_id,),
                )
            return

        with db_session() as conn:
            state = conn.execute(
                "SELECT (SELECT COUNT(*) FROM documents WHERE id = ?) AS document_exists, "
                "(SELECT status FROM tasks WHERE id = ?) AS task_status",
                (doc_id, task_id),
            ).fetchone()
        if not state or not state["document_exists"] or state["task_status"] in ("failed", "cancelled", "cancel_requested"):
            return

        with db_session() as conn:
            conn.execute(
                "UPDATE documents SET status = 'completed', error_msg = NULL WHERE id = ?",
                (doc_id,),
            )
        completed = cls.mark_task_completed(task_id, f"文档解析完成，共生成 {len(chunks)} 个切片")

        # If completed and not cancelled, and document is an exam, answer sheet, or compilation, schedule question extraction
        if completed and not cls.is_cancelled(task_id) and doc_info["file_type"] in ["exam", "answer", "exam_and_answer"]:
            next_task_id = cls.create_task("extract_questions", doc_id, "自动开始提取历年试题与答案", parent_task_id=task_id)
            logger.info("Automatically scheduled question extraction task %s for doc %s (parent %s)", next_task_id, doc_id, task_id)

    @classmethod
    @classmethod
    async def _execute_extract_questions(cls, task_id: str, doc_id: str):
        t0 = time.monotonic()
        cls.log_event(task_id, "stage_change", "开始获取试卷与答案内容切片", stage="read_chunks")
        cls.update_progress_detail(
            task_id,
            stage="read_chunks",
            phase="working",
            unit="batch",
            total=None,
            succeeded=0,
            failed=0,
            skipped=0,
            currentAction="获取试卷与答案内容切片...",
            force=True
        )
        cls.update_task_progress(task_id, 15, "获取试卷与答案内容切片...")
        doc_info = None
        with db_session() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, filename, file_type, year, session_name FROM documents WHERE id = ?", (doc_id,))
            row = cursor.fetchone()
            if row:
                doc_info = dict(row)

        if not doc_info:
            cls.mark_task_failed(task_id, f"文档不存在: {doc_id}")
            return

        with db_session() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, chunk_index, page_number, heading, paragraph_ref, content
                FROM document_chunks
                WHERE doc_id = ? AND parse_error IS NULL AND content <> ''
                ORDER BY chunk_index ASC
            """, (doc_id,))
            chunks = cursor.fetchall()

        if not chunks:
            cls.mark_task_failed(task_id, "该文档没有可提取的文本切片")
            return

        cls.record_timing(task_id, "local_parse", (time.monotonic() - t0) * 1000.0)

        # Batch chunks
        batch_size = 8
        overlap = 1
        step = batch_size - overlap
        chunk_batches = []
        for start in range(0, len(chunks), step):
            batch_batches = chunks[start:start + batch_size]
            if batch_batches:
                chunk_batches.append(batch_batches)
            if start + batch_size >= len(chunks):
                break
        total_batches = len(chunk_batches)

        completed_checkpoints = cls.get_completed_checkpoints("extract_questions", doc_id)

        cls.log_event(task_id, "stage_change", f"开始分批解析试卷（共 {total_batches} 批），逐年识别提取真题与答案", stage="batch_extract")
        cls.update_progress_detail(
            task_id,
            stage="batch_extract",
            phase="working",
            unit="batch",
            total=total_batches,
            succeeded=0,
            failed=0,
            skipped=0,
            currentAction=f"开始分批解析试卷（共 {total_batches} 批）...",
            force=True
        )
        cls.update_task_progress(task_id, 25, f"开始分批解析试卷（共 {total_batches} 个批次），逐年识别提取真题与答案...")

        batch_errors = []
        total_saved_questions = 0

        for b_idx, batch in enumerate(chunk_batches):
            if cls.is_cancelled(task_id):
                cls.finalize_cancellation(task_id)
                return

            await asyncio.sleep(0)
            unit_key = f"qbatch-{doc_id}-{b_idx}"

            # Check checkpoint
            if unit_key in completed_checkpoints:
                cp = completed_checkpoints[unit_key]
                total_saved_questions += cp.get("result_count", 0)
                cls.log_event(task_id, "checkpoint_restored", f"第 {b_idx + 1} 批命中历史检查点，已复用先前提取结果", stage="batch_extract", unit_index=b_idx + 1)
                cls.finish_exec_unit(task_id, f"qbatch-{b_idx + 1}", state="skipped", force=True)
                cls.update_progress_detail(
                    task_id,
                    succeeded=cls._task_details[task_id].get("succeeded", 0) + 1,
                    skipped=cls._task_details[task_id].get("skipped", 0) + 1,
                    resultCount=total_saved_questions,
                    currentAction=f"第 {b_idx + 1} 批已从检查点恢复"
                )
                continue

            pages = sorted({c["page_number"] for c in batch if c["page_number"]})
            scope_str = f"第 {pages[0]}—{pages[-1]} 页" if pages else "切片正文"

            unit_id = f"qbatch-{b_idx + 1}"
            toks = set_unit_context(unit_id=unit_id, unit_index=b_idx + 1, scope=scope_str)
            log_diagnostic(
                "unit_started",
                level=logging.INFO,
                message=f"第 {b_idx + 1} 批题目提取开始 (范围: {scope_str})",
                stage="batch_extract",
                unit_id=unit_id,
                unit_index=b_idx + 1,
                scope=scope_str
            )
            cls.upsert_exec_unit(
                task_id, unit_id,
                kind="batch", index=b_idx + 1, scope=scope_str, service="llm",
                state="waiting_slot", action="等待模型调用名额", force=True
            )
            cls.update_progress_detail(
                task_id,
                phase="waiting_provider",
                currentAction=f"等待第 {b_idx + 1} 批模型响应",
                force=True
            )
            on_llm_event = cls.make_unit_event_handler(
                task_id, unit_id, label=f"第 {b_idx + 1} 批",
                stage="batch_extract", unit_index=b_idx + 1, service="llm"
            )

            # Infer year context
            batch_years = set()
            for c in batch:
                text_snippet = c["content"][:200]
                ym = re.search(r"\b((?:19|20)\d{2})\s*年", text_snippet)
                if ym:
                    try:
                        batch_years.add(int(ym.group(1)))
                    except Exception:
                        pass
            if doc_info.get("year"):
                batch_context_year = doc_info["year"]
            elif len(batch_years) == 1:
                batch_context_year = next(iter(batch_years))
            else:
                batch_context_year = None

            full_text = "\n\n".join(
                [f"[切片 {c['chunk_index']} | {c['heading'] or '段落'}]:\n{c['content']}" for c in batch]
            )
            user_prompt = f"文件类型: {doc_info['file_type']}，文档名称: {doc_info['filename']}，已知参考年份/范围: {doc_info['year'] or '请根据文本各试卷标题推断'}，卷别/科目: {doc_info['session_name'] or '未知'}\n\n当前试卷文本切片如下：\n{full_text}"

            t_req = time.monotonic()
            try:
                res = await LLMClient.chat_json(QUESTION_EXTRACT_SYSTEM_PROMPT, user_prompt, on_event=on_llm_event)
                dur_req = (time.monotonic() - t_req) * 1000.0
                cls.record_timing(task_id, "llm_extract_request", dur_req)

                if cls.is_cancelled(task_id):
                    cls.finalize_cancellation(task_id)
                    return

                cls.upsert_exec_unit(
                    task_id, unit_id,
                    state="saving", action=f"正在校验第 {b_idx + 1} 批题目...", force=True
                )
                t_val = time.monotonic()

                if not isinstance(res, dict) or not isinstance(res.get("questions"), list):
                    raise ValueError("模型返回缺少有效 questions 数组")
                batch_qs = res["questions"]

                validated_batch_qs = []
                for q in batch_qs:
                    if not isinstance(q, dict):
                        raise ValueError("模型返回了无效题目对象")
                    if doc_info["file_type"] == "answer":
                        answer_text = q.get("standard_answer") or q.get("answer_text")
                        if not isinstance(answer_text, str) or not answer_text.strip():
                            raise ValueError("答案候选缺少答案正文")
                        if not isinstance(q.get("stem"), str) or not q["stem"].strip():
                            q["stem"] = q.get("question_num") or "答案候选"
                    elif not isinstance(q.get("stem"), str) or not q["stem"].strip():
                        raise ValueError("模型返回题目缺少有效题干")
                    score = q.get("score")
                    if score is not None:
                        try:
                            score = float(score)
                        except (TypeError, ValueError) as exc:
                            raise ValueError("题目分值不是有效数字") from exc
                        if not math.isfinite(score) or score < 0:
                            raise ValueError("题目分值必须是非负有限数字")
                        q["score"] = score
                    question_type = q.get("question_type") or "short_answer"
                    if question_type not in {"short_answer", "essay", "case_study", "other"}:
                        raise ValueError(f"题型无效: {question_type}")
                    q["question_type"] = question_type
                    for text_field in ("standard_answer", "question_num", "session_name"):
                        value = q.get(text_field)
                        if value is not None and not isinstance(value, str):
                            raise ValueError(f"字段 {text_field} 必须是文本或空值")
                    q_year = q.get("year")
                    normalized_year = None
                    if isinstance(q_year, int) and 1950 <= q_year <= 2050:
                        normalized_year = q_year
                    elif isinstance(q_year, str) and q_year.isdigit() and 1950 <= int(q_year) <= 2050:
                        normalized_year = int(q_year)
                    elif q_year not in (None, ""):
                        raise ValueError(f"题目年份无效: {q_year}")
                    q["final_year"] = normalized_year or batch_context_year
                    source_chunk = batch[0] if len(batch) == 1 else None
                    source_index = q.get("source_chunk_index")
                    if isinstance(source_index, int):
                        source_chunk = next(
                            (item for item in batch if item["chunk_index"] == source_index),
                            None,
                        )
                    q["source_chunk_id"] = source_chunk["id"] if source_chunk else None
                    q["source_page"] = source_chunk["page_number"] if source_chunk else None
                    q["source_paragraph_ref"] = source_chunk["paragraph_ref"] if source_chunk else None
                    answer_chunk = None
                    answer_index = q.get("answer_source_chunk_index")
                    if isinstance(answer_index, int):
                        answer_chunk = next(
                            (item for item in batch if item["chunk_index"] == answer_index),
                            None,
                        )
                    q["answer_source_chunk_id"] = answer_chunk["id"] if answer_chunk else None
                    q["answer_source_doc_id"] = doc_id if answer_chunk else None
                    q["answer_source_page"] = answer_chunk["page_number"] if answer_chunk else None
                    q["answer_source_paragraph_ref"] = answer_chunk["paragraph_ref"] if answer_chunk else None
                    locations = [doc_info["filename"]]
                    batch_p = sorted({c["page_number"] for c in batch if c["page_number"]})
                    batch_r = sorted({c["paragraph_ref"] for c in batch if c["paragraph_ref"]})
                    if batch_p:
                        locations.append("页码 " + ", ".join(str(page) for page in batch_p))
                    if batch_r:
                        locations.extend(batch_r)
                    q["source_location"] = "；".join(locations)
                    validated_batch_qs.append(q)

                cls.record_timing(task_id, "validation", (time.monotonic() - t_val) * 1000.0)

                # Incremental Transactional Save
                t_write = time.monotonic()
                with db_session() as conn:
                    cursor = conn.cursor()
                    for q in validated_batch_qs:
                        stem = q["stem"].strip()
                        year = q.get("final_year")
                        question_num = q.get("question_num")

                        if doc_info["file_type"] == "answer":
                            answer_text = q.get("standard_answer") or q.get("answer_text")
                            if not isinstance(answer_text, str) or not answer_text.strip():
                                raise ValueError(f"答案文件中的题目 {question_num} 缺少答案正文")
                            cursor.execute(
                                "SELECT id FROM answer_candidates WHERE doc_id = ? AND year IS ? "
                                "AND question_num = ? AND answer_text = ?",
                                (doc_id, year, question_num, answer_text.strip()),
                            )
                            if cursor.fetchone():
                                continue
                            cursor.execute("""
                                INSERT INTO answer_candidates (
                                    id, doc_id, year, session_name, question_num, answer_text,
                                    source_chunk_id, source_page, source_paragraph_ref
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, (
                                str(uuid.uuid4()), doc_id, year,
                                q.get("session_name") or doc_info.get("session_name"),
                                question_num, answer_text,
                                q.get("source_chunk_id"), q.get("source_page"), q.get("source_paragraph_ref"),
                            ))
                            continue

                        if q.get("source_chunk_id"):
                            if question_num:
                                cursor.execute(
                                    "SELECT id FROM questions WHERE doc_id = ? AND source_chunk_id = ? "
                                    "AND year IS ? AND session_name IS ? AND question_num = ?",
                                    (doc_id, q["source_chunk_id"], year,
                                     q.get("session_name") or doc_info.get("session_name"), question_num),
                                )
                            else:
                                cursor.execute(
                                    "SELECT id FROM questions WHERE doc_id = ? AND source_chunk_id = ? "
                                    "AND year IS ? AND session_name IS ? AND stem = ?",
                                    (doc_id, q["source_chunk_id"], year,
                                     q.get("session_name") or doc_info.get("session_name"), stem),
                                )
                        else:
                            cursor.execute(
                                "SELECT id FROM questions WHERE doc_id = ? AND year IS ? AND question_num = ? AND stem = ?",
                                (doc_id, year, question_num, stem),
                            )
                        if cursor.fetchone():
                            continue

                        review_reasons = []
                        if year is None:
                            review_reasons.append("年份待补充")
                        if q.get("source_chunk_id") is None:
                            review_reasons.append("来源切片待定位")
                        if q.get("standard_answer") and q.get("answer_source_chunk_id") is None:
                            review_reasons.append("答案来源待定位")
                        if not review_reasons:
                            review_reasons.append("模型提取结果待人工核对")
                        review_reason = "；".join(review_reasons)
                        cursor.execute("""
                            INSERT INTO questions (
                                id, doc_id, year, session_name, question_num, question_type, score,
                                stem, standard_answer, source_location, source_chunk_id, source_page,
                                source_paragraph_ref, answer_source_chunk_id, answer_source_doc_id, answer_source_page,
                                answer_source_paragraph_ref, needs_review, review_reason, is_confirmed, is_ai_generated
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 0, 0)
                        """, (
                            str(uuid.uuid4()), doc_id, year,
                            q.get("session_name") or doc_info.get("session_name"),
                            question_num, q.get("question_type", "short_answer"),
                            q.get("score"), stem, q.get("standard_answer"),
                            q.get("source_location") or f"{doc_info['filename']} ({year or '年份待补充'})",
                            q.get("source_chunk_id"), q.get("source_page"), q.get("source_paragraph_ref"),
                            q.get("answer_source_chunk_id"), q.get("answer_source_doc_id"),
                            q.get("answer_source_page"), q.get("answer_source_paragraph_ref"),
                            review_reason,
                        ))

                cls.record_timing(task_id, "db_write", (time.monotonic() - t_write) * 1000.0)

                # Save checkpoint
                cls.save_checkpoint(
                    "extract_questions", doc_id, unit_key, b_idx, "completed",
                    result_count=len(validated_batch_qs), model_version=settings.LLM_MODEL
                )

                total_saved_questions += len(validated_batch_qs)
                cls.log_event(
                    task_id, "save_done",
                    f"第 {b_idx + 1} 批校验通过，新增保存 {len(validated_batch_qs)} 道题目",
                    stage="batch_extract", unit_index=b_idx + 1
                )
                cls.finish_exec_unit(task_id, unit_id, state="succeeded", force=True)
                cls.update_progress_detail(
                    task_id,
                    phase="working",
                    succeeded=cls._task_details[task_id].get("succeeded", 0) + 1,
                    resultCount=total_saved_questions,
                    currentAction=f"第 {b_idx + 1} 批处理完成，累计 {total_saved_questions} 道题目",
                    force=True
                )
                log_diagnostic(
                    "unit_completed",
                    level=logging.INFO,
                    message=f"第 {b_idx + 1} 批题目提取完成，保存 {len(validated_batch_qs)} 题",
                    stage="batch_extract",
                    unit_id=unit_id,
                    unit_index=b_idx + 1,
                    scope=scope_str,
                    result_count=len(validated_batch_qs)
                )

            except Exception as e:
                dur_req = (time.monotonic() - t_req) * 1000.0
                cls.record_timing(task_id, "llm_extract_request", dur_req)
                safe_err = cls.sanitize_error(e)
                logger.error("Error extracting questions in batch %d: %s", b_idx, safe_err)
                batch_errors.append(f"批次 {b_idx + 1}: {safe_err}")
                cls.save_checkpoint(
                    "extract_questions", doc_id, unit_key, b_idx, "failed",
                    error_msg=safe_err, model_version=settings.LLM_MODEL
                )
                cls.log_event(task_id, "failed", f"第 {b_idx + 1} 批提取失败: {safe_err}", stage="batch_extract", unit_index=b_idx + 1)
                cls.finish_exec_unit(task_id, unit_id, state="failed", error=safe_err, force=True)
                cls.update_progress_detail(
                    task_id,
                    phase="working",
                    failed=cls._task_details[task_id].get("failed", 0) + 1,
                    currentAction=f"第 {b_idx + 1} 批提取失败",
                    force=True
                )
                log_diagnostic(
                    "unit_failed",
                    level=logging.ERROR,
                    message=f"第 {b_idx + 1} 批提取失败: {safe_err}",
                    stage="batch_extract",
                    unit_id=unit_id,
                    unit_index=b_idx + 1,
                    scope=scope_str,
                    exception_type=type(e).__name__
                )
            finally:
                reset_unit_context(toks)

        if batch_errors and total_saved_questions == 0:
            error_text = "所有提取批次均失败；" + "；".join(batch_errors)
            with db_session() as conn:
                conn.execute(
                    "UPDATE tasks SET batch_errors_json = ? WHERE id = ?",
                    (json.dumps(batch_errors, ensure_ascii=False), task_id),
                )
            cls.mark_task_failed(task_id, error_text)
            return

        if total_saved_questions == 0 and not batch_errors:
            cls.mark_task_failed(task_id, "模型未识别到题目，请核对原文切片或调整资料类型")
            return

        if cls.is_cancelled(task_id):
            cls.finalize_cancellation(task_id)
            return

        if batch_errors:
            error_text = f"部分批次提取失败（共 {len(batch_errors)} 批次异常，成功提取 {total_saved_questions} 道试题）；" + "；".join(batch_errors)
            with db_session() as conn:
                conn.execute(
                    "UPDATE tasks SET batch_errors_json = ? WHERE id = ?",
                    (json.dumps(batch_errors, ensure_ascii=False), task_id),
                )
            cls.mark_task_partial(task_id, error_text)
        else:
            cls.mark_task_completed(task_id, f"成功完成提取，共识别 {total_saved_questions} 道试题（含题目与对应答案），已进入待人工确认池")

    @classmethod
    async def _execute_generate_card(cls, task_id: str, knowledge_point_id: str):
        if cls.is_cancelled(task_id):
            cls.finalize_cancellation(task_id)
            return

        t0 = time.monotonic()
        cls.log_event(task_id, "stage_change", "检索考点关联资料与真题", stage="retrieve_context")
        cls.update_progress_detail(
            task_id,
            stage="retrieve_context",
            phase="working",
            unit="chunk",
            total=1,
            succeeded=0,
            failed=0,
            skipped=0,
            currentAction="正在检索考点关联资料与真题...",
            force=True
        )
        cls.update_task_progress(task_id, 15, "正在检索考点关联资料与真题...")

        kp = None
        with db_session() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, name, chapter FROM knowledge_points WHERE id = ?", (knowledge_point_id,))
            row = cursor.fetchone()
            if row:
                kp = dict(row)

        if not kp:
            cls.mark_task_failed(task_id, "知识点不存在")
            return

        if cls.is_cancelled(task_id):
            cls.finalize_cancellation(task_id)
            return

        # 1. Retrieve related textbook chunks via hybrid search
        query = f"{kp['name']} {kp['chapter'] or ''}"
        retrieved_chunks = await HybridSearchService.search(query, top_k=5)

        # 2. Retrieve confirmed past exam questions for this knowledge point
        past_questions = []
        with db_session() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT q.year, q.stem, q.standard_answer
            FROM question_knowledge_rel rel
            JOIN questions q ON rel.question_id = q.id
            WHERE rel.knowledge_point_id = ? AND q.is_confirmed = 1
            """, (knowledge_point_id,))
            past_questions = [dict(r) for r in cursor.fetchall()]

        cls.record_timing(task_id, "local_parse", (time.monotonic() - t0) * 1000.0)

        if cls.is_cancelled(task_id):
            cls.finalize_cancellation(task_id)
            return

        cls.log_event(task_id, "stage_change", "组织模型构建四维学习卡", stage="llm_generate")
        cls.update_progress_detail(
            task_id,
            stage="llm_generate",
            phase="waiting_provider",
            currentAction="正在组织模型构建四维学习卡...",
            force=True
        )
        cls.upsert_exec_unit(
            task_id, "card",
            kind="card", index=1, scope=kp["name"], service="llm",
            state="waiting_slot", action="等待模型调用名额", force=True
        )
        cls.update_task_progress(task_id, 50, "正在组织模型构建四维学习卡...")

        chunks_context = ""
        citations = []
        for c in retrieved_chunks:
            loc = f"第 {c['page_number']} 页" if c['page_number'] else (c['paragraph_ref'] or "段落")
            chunks_context += f"\n--- 资料来源: {c['doc_name']} ({loc}) ---\n{c['content']}\n"
            citations.append({
                "chunk_id": c["chunk_id"],
                "doc_name": c["doc_name"],
                "page_or_ref": loc,
                "snippet": c["content"][:120] + "..."
            })

        questions_context = ""
        for pq in past_questions:
            questions_context += f"\n【{pq['year']}年真题】: {pq['stem']}\n【已有答案】: {pq['standard_answer'] or '暂无'}\n"

        user_prompt = f"""知识点名称: {kp['name']}
所属章节: {kp['chapter'] or '未指定'}

【检索到的教材切片资料】:
{chunks_context or '无相关教材切片'}

【历年真题及答案考查记录】:
{questions_context or '暂无已确认历史真题'}
"""

        on_card_event = cls.make_unit_event_handler(
            task_id, "card", label="学习卡生成",
            stage="llm_generate", unit_index=None, service="llm"
        )

        t_llm = time.monotonic()
        try:
            res = await LLMClient.chat_json(STUDY_CARD_SYSTEM_PROMPT, user_prompt, on_event=on_card_event)
        except Exception as exc:
            cls.finish_exec_unit(task_id, "card", state="failed", error=str(exc), force=True)
            raise
        finally:
            cls.record_timing(task_id, "llm_extract_request", (time.monotonic() - t_llm) * 1000.0)

        if cls.is_cancelled(task_id):
            cls.finalize_cancellation(task_id)
            return

        cls.log_event(task_id, "stage_change", "保存学习卡", stage="save_card")
        cls.update_progress_detail(
            task_id,
            stage="save_card",
            phase="working",
            currentAction="正在保存学习卡...",
            force=True
        )
        cls.upsert_exec_unit(
            task_id, "card",
            state="saving", action="正在保存学习卡...", force=True
        )
        cls.update_task_progress(task_id, 90, "正在保存学习卡...")

        card_id = str(uuid.uuid4())
        t_save = time.monotonic()
        with db_session() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            INSERT INTO study_cards (
                id, knowledge_point_id, concise_content, detailed_content, 
                answer_structure, confusing_points_and_cases, citations_json, needs_update, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, CURRENT_TIMESTAMP)
            ON CONFLICT(knowledge_point_id) DO UPDATE SET
                concise_content = excluded.concise_content,
                detailed_content = excluded.detailed_content,
                answer_structure = excluded.answer_structure,
                confusing_points_and_cases = excluded.confusing_points_and_cases,
                citations_json = excluded.citations_json,
                needs_update = 0,
                updated_at = CURRENT_TIMESTAMP
            """, (
                card_id,
                knowledge_point_id,
                res.get("concise_content", ""),
                res.get("detailed_content", ""),
                res.get("answer_structure", ""),
                res.get("confusing_points_and_cases", ""),
                json.dumps(citations, ensure_ascii=False)
            ))
        cls.record_timing(task_id, "db_write", (time.monotonic() - t_save) * 1000.0)
        cls.finish_exec_unit(task_id, "card", state="succeeded", force=True)
        cls.update_progress_detail(task_id, succeeded=1, resultCount=1, force=True)
        cls.mark_task_completed(task_id, f"学习卡已成功生成")
