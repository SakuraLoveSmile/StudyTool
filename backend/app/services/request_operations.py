"""Durable idempotency for long running synchronous HTTP operations."""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from typing import Any, Awaitable, Callable, TypeVar

from fastapi import HTTPException

from app.database import db_session
from app.config import settings
from app.tenancy import current_tenant_id

T = TypeVar("T")

_tasks: dict[tuple[str, str, str], asyncio.Task[Any]] = {}
_lock = asyncio.Lock()


def _payload_hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _ensure_table() -> None:
    with db_session() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS request_operations (
              id TEXT PRIMARY KEY,
              route TEXT NOT NULL,
              idempotency_key TEXT NOT NULL,
              payload_hash TEXT NOT NULL,
              state TEXT NOT NULL,
              result_json TEXT,
              error_json TEXT,
              created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
              UNIQUE(route, idempotency_key)
            )
        """)


async def run_idempotent(
    route: str,
    idempotency_key: str | None,
    payload: Any,
    execute: Callable[[], Awaitable[T]],
    response_type: type[T],
) -> T:
    """Run once per tenant/route/key and replay the exact response on retry."""
    # Direct function calls in the existing test suite do not run FastAPI's
    # dependency injection, so Header(None) can arrive here as a marker.
    if not isinstance(idempotency_key, str) or not idempotency_key:
        return await execute()
    if len(idempotency_key) > 255:
        raise HTTPException(status_code=400, detail="Idempotency-Key too long")

    tenant = current_tenant_id() or "__default__"
    # Test and local deployments can swap DATA_DIR while the process lives.
    tenant = f"{tenant}:{settings.db_path}"
    task_key = (tenant, route, idempotency_key)
    fingerprint = _payload_hash(payload)
    async with _lock:
        _ensure_table()
        with db_session() as conn:
            row = conn.execute(
                "SELECT * FROM request_operations WHERE route=? AND idempotency_key=?",
                (route, idempotency_key),
            ).fetchone()
            if row and row["payload_hash"] != fingerprint:
                raise HTTPException(status_code=409, detail="Idempotency-Key was used with a different request")
            if row and row["state"] == "completed" and row["result_json"]:
                return response_type.model_validate(json.loads(row["result_json"]))  # type: ignore[attr-defined]
            if row and row["state"] in ("pending", "running") and task_key not in _tasks:
                # A process restart cannot safely claim an old result.
                conn.execute("UPDATE request_operations SET state='failed', error_json=? WHERE id=?", (json.dumps({"detail": "operation interrupted"}), row["id"]))
                row = None
            if not row or row["state"] == "failed":
                op_id = row["id"] if row else str(uuid.uuid4())
                conn.execute(
                    "INSERT OR REPLACE INTO request_operations(id,route,idempotency_key,payload_hash,state,error_json,updated_at) VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                    (op_id, route, idempotency_key, fingerprint, "running", None),
                )

    existing = _tasks.get(task_key)
    if existing:
        return await asyncio.shield(existing)

    async def wrapped() -> T:
            try:
                result = await execute()
                encoded = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
                with db_session() as conn:
                    conn.execute("UPDATE request_operations SET state='completed', result_json=?, updated_at=CURRENT_TIMESTAMP WHERE route=? AND idempotency_key=?", (json.dumps(encoded, ensure_ascii=False), route, idempotency_key))
                return result
            except asyncio.CancelledError:
                with db_session() as conn:
                    conn.execute("UPDATE request_operations SET state='failed', error_json=?, updated_at=CURRENT_TIMESTAMP WHERE route=? AND idempotency_key=?", (json.dumps({"detail": "operation cancelled"}), route, idempotency_key))
                raise
            except Exception as exc:
                with db_session() as conn:
                    conn.execute("UPDATE request_operations SET state='failed', error_json=?, updated_at=CURRENT_TIMESTAMP WHERE route=? AND idempotency_key=?", (json.dumps({"detail": type(exc).__name__}), route, idempotency_key))
                raise

    task = asyncio.create_task(wrapped())
    _tasks[task_key] = task
    def finished(done):
        if _tasks.get(task_key) is done:
            _tasks.pop(task_key, None)
        # A disconnected caller may no longer be awaiting a failed result.
        if not done.cancelled():
            done.exception()
    task.add_done_callback(finished)
    return await asyncio.shield(task)


async def stop_operations() -> None:
    """Cancel and await in-process operations during application shutdown."""
    tasks = list(_tasks.values())
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _tasks.clear()


async def stop_tenant_operations(user_id: str) -> None:
    tasks = [task for key, task in list(_tasks.items()) if key[0].startswith(user_id + ":")]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
