"""Durable chapter jobs: isolated SQLite and fake model calls, never paid APIs."""
import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.config import settings
from app.database import db_session, init_db
from app.diagnostics import task_id_var
from app.models.library import GuideRequest
from app.routers.library import router
from app.routers.materials import get_task_history, get_task_status, retry_task
from app.services import library
from app.services.task_worker import TaskWorker
from app.tenancy import tenant_scope


def _seed():
    with db_session() as conn:
        for doc, kind in (("accounting", "textbook"), ("management", "textbook"), ("exam", "exam")):
            conn.execute("INSERT INTO documents(id,filename,file_hash,file_type,status) VALUES(?,?,?,?, 'completed')",
                         (doc, doc + ".docx", doc, kind))
            conn.execute("INSERT INTO document_chunks(id,doc_id,chunk_index,heading,content) VALUES(?,?,0,?,?)",
                         (doc + "-chunk", doc, "第一章 会计", "会计是经济管理活动。预算控制帮助经营计划。"))
    return library.list_chapters("accounting")[0].id


@pytest.fixture
def chapter(monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    monkeypatch.setattr(settings, "LLM_API_KEY", "fake-key-never-network")
    monkeypatch.delenv("DEMO_MODE", raising=False)
    return _seed()


def _reply(prompt):
    data = json.loads(prompt)
    chunk = data["complete_accounting_chapter"][0]
    citation = {"chunk_id": chunk["chunk_id"], "quote": chunk["passages"][0]["content"][:10]}
    return {"summary": "会计基本理论", "concepts": [{"name": "会计", "explanation": "管理活动",
        "recitation": "会计是经济管理活动。", "keywords": ["管理"], "citations": [citation]}],
        "mind_map": {"title": "会计", "children": [{"title": "会计", "children": []}]},
        "connections": [], "citations": [citation]}


def _job(chapter, **kwargs):
    return dict(library.enqueue_guide(GuideRequest(doc_id="accounting", chapter_id=chapter, **kwargs)))


def _events(task_id):
    with db_session() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM task_events WHERE task_id=? ORDER BY id", (task_id,))]


def test_enqueue_is_immediate_visible_and_idempotent(chapter, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Submission must not wait for or call the model")
    monkeypatch.setattr(library.LLMClient, "chat_json", forbidden)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    body = {"doc_id": "accounting", "chapter_id": chapter}
    first = client.post("/api/library/guides/tasks", json=body, headers={"Idempotency-Key": "one"})
    assert first.status_code == 202
    task = first.json()
    assert task["status"] == "pending" and task["task_type"] == "generate_library_guide"
    assert client.post("/api/library/guides/tasks", json=body, headers={"Idempotency-Key": "one"}).json()["id"] == task["id"]
    assert client.post("/api/library/guides/tasks", json=body).json()["id"] == task["id"]
    assert [t.id for t in get_task_history().tasks] == [task["id"]]
    listing = client.get("/api/library/guides/tasks", params=body)
    assert listing.status_code == 200 and listing.json()[0]["id"] == task["id"]
    assert client.get("/api/library/guides/tasks", params={**body, "chapter_id": "outside"}).status_code == 404
    assert client.post("/api/library/guides/tasks", json={**body, "management_doc_ids": ["management"]},
                       headers={"Idempotency-Key": "one"}).status_code == 409


@pytest.mark.asyncio
async def test_worker_loads_persisted_request_saves_and_logs(chapter, monkeypatch):
    task = _job(chapter, management_doc_ids=["management"], exam_doc_ids=["exam"])
    TaskWorker._task_details.pop(task["id"], None)  # process-local state is optional
    calls = []
    async def model(system, prompt, on_event=None):
        calls.append(json.loads(prompt))
        assert task_id_var.get() == task["id"]
        assert get_task_status(task["id"]).progress == 30
        assert get_task_status(task["id"]).progress_detail.activeUnits == [1]
        await on_event("request_start", {"attempt": 1})
        await on_event("response_received", {})
        return _reply(prompt)
    monkeypatch.setattr(library.LLMClient, "chat_json", model)
    await TaskWorker._dispatch_task(task)
    assert get_task_status(task["id"]).status == "completed"
    assert len(library.list_guides("accounting", chapter)) == 1
    assert calls[0]["management_excerpts"][0]["chunk_id"] == "management-chunk"
    events = _events(task["id"])
    kinds = [e["event_type"] for e in events]
    assert kinds == ["created", "reading_sources", "model_call", "request_start", "response_received", "validation", "saving", "completed"]
    assert {e["stage"] for e in events} >= {"read_sources", "llm_generate", "save_guide"}
    assert TaskWorker.request_cancel(task["id"])["status"] == "completed"
    later = _job(chapter, management_doc_ids=["management"], exam_doc_ids=["exam"])
    assert later["id"] != task["id"]
    assert [r["id"] for r in library.list_guide_tasks("accounting", chapter)] == [later["id"], task["id"]]


@pytest.mark.asyncio
async def test_timeout_reason_retry_and_request_parameters_survive(chapter, monkeypatch):
    task = _job(chapter, management_doc_ids=["management"], exam_doc_ids=["exam"])
    async def failure(system, prompt, on_event=None):
        await on_event("request_start", {"attempt": 1})
        await on_event("retry_wait", {"attempt": 1, "wait_seconds": 2, "reason": "网络超时 (ReadTimeout)"})
        try:
            raise httpx.ReadTimeout("secret原文sk-secret-and-provider-body")
        except httpx.ReadTimeout as exc:
            raise RuntimeError("do not expose raw provider secret") from exc
    monkeypatch.setattr(library.LLMClient, "chat_json", failure)
    await TaskWorker._dispatch_task(task)
    failed = get_task_status(task["id"])
    assert failed.status == "failed" and "ReadTimeout" in failed.error_msg
    assert failed.progress_detail.failed == 1
    assert "secret" not in failed.error_msg
    assert any(e["event_type"] == "retry_wait" and "ReadTimeout" in e["message"] for e in _events(task["id"]))
    assert library.list_guides() == []
    successor = retry_task(task["id"])
    assert successor.target_id == task["target_id"] and successor.retry_of == task["id"]
    assert retry_task(task["id"]).id == successor.id
    TaskWorker._task_details.pop(successor.id, None)
    async def success(system, prompt, on_event=None):
        assert json.loads(prompt)["management_excerpts"][0]["chunk_id"] == "management-chunk"
        return _reply(prompt)
    monkeypatch.setattr(library.LLMClient, "chat_json", success)
    with db_session() as conn:
        persisted = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (successor.id,)).fetchone())
    await TaskWorker._dispatch_task(persisted)
    assert get_task_status(successor.id).status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("suppress_cancel", [False, True])
async def test_cancel_during_model_cannot_save_result(chapter, monkeypatch, suppress_cancel):
    task = _job(chapter)
    started = asyncio.Event()
    async def model(system, prompt, on_event=None):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if not suppress_cancel:
                raise
            return _reply(prompt)
    monkeypatch.setattr(library.LLMClient, "chat_json", model)
    running = asyncio.create_task(TaskWorker._dispatch_task(task))
    await asyncio.wait_for(started.wait(), 2)
    TaskWorker.request_cancel(task["id"])
    await asyncio.wait_for(running, 2)
    assert get_task_status(task["id"]).status == "cancelled"
    assert get_task_status(task["id"]).progress_detail.failed == 0
    assert library.list_guides() == []
    assert not any(e["event_type"] == "completed" for e in _events(task["id"]))


@pytest.mark.asyncio
async def test_cancel_at_save_boundary_is_atomic(chapter, monkeypatch):
    task = _job(chapter)
    async def model(system, prompt, on_event=None):
        return _reply(prompt)
    monkeypatch.setattr(library.LLMClient, "chat_json", model)
    original = library._save
    def cancel_then_save(*args, **kwargs):
        TaskWorker.request_cancel(task["id"])
        return original(*args, **kwargs)
    monkeypatch.setattr(library, "_save", cancel_then_save)
    await TaskWorker._dispatch_task(task)
    assert get_task_status(task["id"]).status == "cancelled"
    assert library.list_guides() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["queued", "during_model"])
async def test_changed_snapshot_cannot_publish_result(chapter, monkeypatch, when):
    task = _job(chapter)
    def change():
        with db_session() as conn:
            conn.execute("UPDATE document_chunks SET content=content||'编辑后' WHERE doc_id='accounting'")
    if when == "queued":
        change()
    async def model(system, prompt, on_event=None):
        assert when == "during_model", "Changed queued snapshot must fail before any paid call"
        change()
        return _reply(prompt)
    monkeypatch.setattr(library.LLMClient, "chat_json", model)
    await TaskWorker._dispatch_task(task)
    assert get_task_status(task["id"]).status == "failed"
    assert "来源资料" in get_task_status(task["id"]).error_msg
    assert library.list_guides() == []


def test_same_key_survives_source_changes_and_concurrent_clicks(chapter):
    from concurrent.futures import ThreadPoolExecutor
    req = GuideRequest(doc_id="accounting", chapter_id=chapter)
    with ThreadPoolExecutor(max_workers=3) as pool:
        ids = list(pool.map(lambda _: library.enqueue_guide(req, "same-key")["id"], range(3)))
    assert len(set(ids)) == 1
    with db_session() as conn:
        conn.execute("UPDATE tasks SET status='failed' WHERE id=?", (ids[0],))
        conn.execute("DELETE FROM documents WHERE id='accounting'")
    assert library.enqueue_guide(req, "same-key")["id"] == ids[0]


@pytest.mark.asyncio
async def test_cancel_pending_skips_model(chapter, monkeypatch):
    task = _job(chapter)
    async def forbidden(*args, **kwargs):
        pytest.fail("A cancelled queued job must never call a paid model")
    monkeypatch.setattr(library.LLMClient, "chat_json", forbidden)
    TaskWorker.request_cancel(task["id"])
    await TaskWorker._dispatch_task(task)
    assert get_task_status(task["id"]).status == "cancelled"
    assert library.list_guides() == []


def test_task_scope_and_idempotency_are_tenant_local():
    users = ["00000000-0000-0000-0000-000000000011", "00000000-0000-0000-0000-000000000012"]
    jobs = []
    for user in users:
        with tenant_scope(user):
            init_db()
            chapter = _seed()
            jobs.append(dict(library.enqueue_guide(GuideRequest(doc_id="accounting", chapter_id=chapter), "same")))
    with tenant_scope(users[0]):
        assert [t["id"] for t in library.list_guide_tasks("accounting", chapter)] == [jobs[0]["id"]]
        with pytest.raises(HTTPException) as error:
            get_task_status(jobs[1]["id"])
        assert error.value.status_code == 404
