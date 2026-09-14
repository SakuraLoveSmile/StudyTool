from contextlib import contextmanager
from types import SimpleNamespace
import asyncio

import pytest

from app.config import settings
from app.services.retrieval.embedding import EmbeddingService
from app.services.task_worker import TaskWorker


@pytest.fixture
def tenancy(monkeypatch):
    current = {"id": None}

    def current_tenant_id():
        return current["id"]

    @contextmanager
    def tenant_scope(user_id):
        old = current["id"]
        current["id"] = user_id
        try:
            yield
        finally:
            current["id"] = old

    monkeypatch.setattr("app.services.task_worker._current_tenant_id", current_tenant_id)
    monkeypatch.setitem(__import__("sys").modules, "app.tenancy", SimpleNamespace(
        current_tenant_id=current_tenant_id, tenant_scope=tenant_scope,
    ))
    monkeypatch.setattr(settings, "PUBLIC_MODE", True, raising=False)
    TaskWorker._tenant_workers.clear()
    yield tenant_scope
    TaskWorker._tenant_workers.clear()


def test_task_worker_state_isolated_per_tenant(tenancy):
    with tenancy("user-a"):
        worker_a = TaskWorker._worker_for_tenant("user-a")
        worker_a._task_details["a"] = {"owner": "user-a"}
        assert TaskWorker._worker_for_tenant("user-a")._task_details["a"]["owner"] == "user-a"

    with tenancy("user-b"):
        worker_b = TaskWorker._worker_for_tenant("user-b")
        assert worker_b is not worker_a
        assert "a" not in worker_b._task_details
        worker_b._task_details["b"] = {"owner": "user-b"}

    with tenancy("user-a"):
        assert TaskWorker._worker_for_tenant("user-a")._task_details["a"]["owner"] == "user-a"
        assert "b" not in TaskWorker._task_details


def test_embedding_cache_key_includes_tenant(tenancy):
    with tenancy("user-a"):
        key_a = EmbeddingService._cache_key("same text")
    with tenancy("user-b"):
        key_b = EmbeddingService._cache_key("same text")
    assert key_a != key_b


def test_public_worker_requires_tenant(tenancy):
    with pytest.raises(RuntimeError, match="tenant context"):
        TaskWorker.create_task("parse_document", "missing")


@pytest.mark.asyncio
async def test_manager_starts_new_enabled_tenant(monkeypatch, tenancy):
    seen = []

    class FakeWorker:
        _running = False
        _tenant_id = "user-a"
        _worker_task = None
        _heartbeat_task = None
        _running_tasks = {}

        def start(self):
            self._running = True
            seen.append((self._tenant_id, "start"))

        def stop(self):
            self._running = False

    worker = FakeWorker()
    monkeypatch.setattr("app.services.task_worker._public_mode", lambda: True)
    monkeypatch.setitem(__import__("sys").modules, "app.auth", SimpleNamespace(
        enabled_user_ids=lambda: ["user-a"],
    ))
    monkeypatch.setattr(TaskWorker, "_worker_for_tenant", classmethod(lambda cls, _: worker))

    async def stop_after_first_iteration(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", stop_after_first_iteration)
    with pytest.raises(asyncio.CancelledError):
        await TaskWorker._tenant_manager_loop()
    assert seen == [("user-a", "start")]
