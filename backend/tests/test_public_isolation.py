"""Public-mode acceptance checks for authentication and tenant isolation.

These tests seed only synthetic rows in per-user databases.  They deliberately
do not invoke OCR, LLM, or embedding providers.
"""
from __future__ import annotations

import hashlib
import uuid
import pytest
from fastapi.testclient import TestClient

from app import auth
from app.config import settings
from app.database import db_session
from app.main import app
from app.tenancy import tenant_scope
from app.services.learning_service import LearningService
from app.services.recall_cards import RecallCardsService


ORIGIN = "https://testserver"
PASSWORD_A = "alice-password-123"
PASSWORD_B = "bob-password-123"


@pytest.fixture()
def public_env(tmp_path, monkeypatch):
    """Create a clean account root and two independent tenant databases."""
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "LOG_DIR", str(tmp_path / "logs"), raising=False)
    monkeypatch.setattr(settings, "PUBLIC_MODE", True, raising=False)
    monkeypatch.setattr(settings, "PUBLIC_ORIGIN", ORIGIN, raising=False)
    monkeypatch.setattr(settings, "AUTH_COOKIE_SECURE", True, raising=False)
    monkeypatch.setattr(settings, "DEMO_MODE", False, raising=False)
    monkeypatch.setattr(settings, "OCR_MODE", "vision_llm", raising=False)
    monkeypatch.setattr(settings, "EMBEDDING_MODE", "remote", raising=False)
    auth.init_auth_db()
    alice = auth.provision_user("alice", PASSWORD_A)
    bob = auth.provision_user("bob", PASSWORD_B)
    return tmp_path, alice, bob


def _login(client: TestClient, username: str, password: str) -> tuple[str, str]:
    response = client.post(
        "/api/auth/login",
        headers={"Origin": ORIGIN},
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["user"]["must_change_password"] is True
    csrf = payload["csrf_token"]
    changed = client.post(
        "/api/auth/change-password",
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        json={"current_password": password, "new_password": password + "-changed"},
    )
    assert changed.status_code == 200, changed.text
    return changed.json()["csrf_token"], client.cookies.get("rag_session")


def _seed_document(user_id: str, label: str) -> tuple[str, str, str, str]:
    doc_id = str(uuid.uuid4())
    chunk_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    content = f"synthetic private content {label}"
    digest = hashlib.sha256(f"{label}.pdf".encode()).hexdigest()
    with tenant_scope(user_id):
        upload = settings.uploads_path / f"{digest}.pdf"
        upload.write_bytes(f"file-{label}".encode())
        with db_session() as conn:
            conn.execute(
                "INSERT INTO documents (id, filename, file_hash, file_type, status) VALUES (?, ?, ?, ?, 'completed')",
                (doc_id, f"{label}.pdf", digest, "textbook"),
            )
            conn.execute(
                "INSERT INTO document_chunks (id, doc_id, chunk_index, content, content_segmented) VALUES (?, ?, 0, ?, ?)",
                (chunk_id, doc_id, content, content),
            )
            conn.execute(
                "INSERT INTO document_chunks_fts (chunk_id, content_segmented) VALUES (?, ?)",
                (chunk_id, content),
            )
            conn.execute(
                "INSERT INTO tasks (id, task_type, target_id, status, message) VALUES (?, 'parse_document', ?, 'failed', ?)",
                (task_id, doc_id, f"synthetic failed task {label}"),
            )
            conn.execute(
                "INSERT INTO task_events (task_id, event_type, message) VALUES (?, 'failed', ?)",
                (task_id, f"synthetic event {label}"),
            )
    return doc_id, chunk_id, digest, task_id


def _seed_published_card(user_id: str, label: str) -> str:
    with tenant_scope(user_id), db_session() as conn:
        LearningService.update_settings(
            {"daily_new_limit": 5, "daily_review_limit": 5, "timezone": "Asia/Shanghai"}, conn=conn
        )
        chapter = RecallCardsService.create_chapter({"title": f"{label} chapter"}, conn=conn)
        card = RecallCardsService.create_card(
            {"chapter_id": chapter["id"], "prompt": f"{label} prompt", "answer": f"{label} answer"}, conn=conn
        )
        RecallCardsService.review_revisions(
            {"revisions": [{"revision_id": card["revision_id"], "decision": "approve"}]}, conn=conn
        )
        published = RecallCardsService.publish_card(card["id"], revision_id=card["revision_id"], conn=conn)
        return published["id"]


def test_public_auth_requires_change_password_and_same_origin_csrf(public_env):
    _, _, _ = public_env
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/api/materials").status_code == 401
        csrf, _ = _login(client, "alice", PASSWORD_A)
        assert client.get("/api/auth/me").json()["user"]["username"] == "alice"
        assert client.get("/api/config/status").status_code == 200
        assert client.post("/api/auth/logout", headers={"Origin": ORIGIN}).status_code == 403
        assert client.post(
            "/api/auth/logout",
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        ).json() == {"ok": True}
        assert client.get("/api/auth/me").status_code == 401


def test_public_cross_tenant_documents_are_invisible_and_independent(public_env):
    _, alice_id, bob_id = public_env
    alice_doc, alice_chunk, _, alice_task = _seed_document(alice_id, "alice")
    bob_doc, bob_chunk, _, bob_task = _seed_document(bob_id, "bob")
    with TestClient(app, base_url=ORIGIN) as client:
        alice_csrf, alice_token = _login(client, "alice", PASSWORD_A)
        alice_docs = client.get("/api/materials").json()
        bob_csrf, bob_token = _login(client, "bob", PASSWORD_B)
        bob_docs = client.get("/api/materials").json()
        assert [row["id"] for row in alice_docs] == [alice_doc]
        assert [row["id"] for row in bob_docs] == [bob_doc]
        client.cookies.clear()
        client.cookies.set("rag_session", alice_token)
        assert client.get(f"/api/materials/{bob_doc}/chunks").status_code == 404
        assert client.get(f"/api/materials/{bob_doc}/file").status_code == 404
        cross_mutation = client.put(
            f"/api/materials/chunks/{bob_chunk}",
            headers={"Origin": ORIGIN, "X-CSRF-Token": alice_csrf},
            json={"content": "must not change bob"},
        )
        assert cross_mutation.status_code == 404
        cross_delete = client.delete(
            f"/api/materials/{bob_doc}",
            headers={"Origin": ORIGIN, "X-CSRF-Token": alice_csrf},
        )
        assert cross_delete.status_code == 404
        client.cookies.clear()
        client.cookies.set("rag_session", bob_token)
        assert [row["id"] for row in client.get("/api/materials").json()] == [bob_doc]

        client.cookies.clear()
        client.cookies.set("rag_session", alice_token)
        assert client.get(f"/api/materials/tasks/{bob_task}").status_code == 404
        assert client.get(f"/api/materials/tasks/{bob_task}/events").status_code == 404
        retry = client.post(
            f"/api/materials/tasks/{bob_task}/retry",
            headers={"Origin": ORIGIN, "X-CSRF-Token": alice_csrf},
        )
        assert retry.status_code == 404
        assert all(row["id"] != bob_task for row in client.get("/api/materials/tasks/list").json())
        client.cookies.clear()
        client.cookies.set("rag_session", bob_token)
        assert client.get(f"/api/materials/tasks/{alice_task}").status_code == 404


def test_public_rejects_wrong_origin_and_missing_tenant_storage(public_env, monkeypatch):
    with TestClient(app, base_url=ORIGIN) as client:
        csrf, _ = _login(client, "alice", PASSWORD_A)
        response = client.put(
            "/api/learning/settings",
            headers={"Origin": "https://evil.example", "X-CSRF-Token": csrf},
            json={"daily_new_limit": 3},
        )
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "ORIGIN_REJECTED"
    monkeypatch.setattr(settings, "PUBLIC_MODE", True, raising=False)
    from app.tenancy import current_tenant_id

    assert current_tenant_id() is None
    with pytest.raises(RuntimeError, match="Tenant context required"):
        _ = settings.db_path


def test_disabled_user_session_is_revoked(public_env):
    _, _, _ = public_env
    with TestClient(app, base_url=ORIGIN) as client:
        _login(client, "bob", PASSWORD_B)
        assert client.get("/api/auth/me").status_code == 200
        auth.disable_user("bob")
        assert client.get("/api/auth/me").status_code == 401
        assert client.get("/api/materials").status_code == 401


def test_public_learning_attempts_and_idempotency_are_tenant_scoped(public_env):
    _, alice_id, bob_id = public_env
    alice_card = _seed_published_card(alice_id, "alice")
    bob_card = _seed_published_card(bob_id, "bob")
    with TestClient(app, base_url=ORIGIN) as client:
        alice_csrf, alice_token = _login(client, "alice", PASSWORD_A)
        client.post("/api/learning/today/open", headers={"Origin": ORIGIN, "X-CSRF-Token": alice_csrf}, json={})
        alice_today = client.get("/api/learning/today")
        assert alice_today.status_code == 200, alice_today.text
        alice_attempt = alice_today.json()["next_item"]["attempt_id"]
        assert alice_today.json()["next_item"]["card_id"] == alice_card
        bob_csrf, bob_token = _login(client, "bob", PASSWORD_B)
        client.post("/api/learning/today/open", headers={"Origin": ORIGIN, "X-CSRF-Token": bob_csrf}, json={})
        bob_today = client.get("/api/learning/today")
        assert bob_today.status_code == 200, bob_today.text
        bob_attempt = bob_today.json()["next_item"]["attempt_id"]
        assert bob_today.json()["next_item"]["card_id"] == bob_card

        client.cookies.clear(); client.cookies.set("rag_session", alice_token)
        cross_reveal = client.post(
            f"/api/learning/attempts/{bob_attempt}/reveal",
            headers={"Origin": ORIGIN, "X-CSRF-Token": alice_csrf}, json={}
        )
        assert cross_reveal.status_code in {404, 400, 422}
        assert client.post(
            f"/api/learning/attempts/{alice_attempt}/reveal",
            headers={"Origin": ORIGIN, "X-CSRF-Token": alice_csrf}, json={}
        ).status_code == 200
        rated_a = client.post(
            f"/api/learning/attempts/{alice_attempt}/rate",
            headers={"Origin": ORIGIN, "X-CSRF-Token": alice_csrf},
            json={"rating": "mastered", "request_id": "same-request-id"},
        )
        assert rated_a.status_code == 200, rated_a.text

        client.cookies.clear(); client.cookies.set("rag_session", bob_token)
        cross_rate = client.post(
            f"/api/learning/attempts/{alice_attempt}/rate",
            headers={"Origin": ORIGIN, "X-CSRF-Token": bob_csrf},
            json={"rating": "mastered", "request_id": "bob-cross"},
        )
        assert cross_rate.status_code in {404, 400, 422}
        assert client.post(
            f"/api/learning/attempts/{bob_attempt}/reveal",
            headers={"Origin": ORIGIN, "X-CSRF-Token": bob_csrf}, json={}
        ).status_code == 200
        rated_b = client.post(
            f"/api/learning/attempts/{bob_attempt}/rate",
            headers={"Origin": ORIGIN, "X-CSRF-Token": bob_csrf},
            json={"rating": "mastered", "request_id": "same-request-id"},
        )
        assert rated_b.status_code == 200, rated_b.text
        replay = client.post(
            f"/api/learning/attempts/{bob_attempt}/rate",
            headers={"Origin": ORIGIN, "X-CSRF-Token": bob_csrf},
            json={"rating": "mastered", "request_id": "same-request-id"},
        )
        assert replay.status_code == 200
        assert replay.json().get("idempotent_replay") is True


@pytest.mark.asyncio
async def test_real_workers_restart_and_disable_keep_tenant_context(public_env, monkeypatch):
    import asyncio
    from app.services.task_worker import TaskWorker
    from app.tenancy import current_tenant_id
    _, alice_id, bob_id = public_env
    started = {uid: asyncio.Event() for uid in (alice_id, bob_id)}
    cancelled = {uid: asyncio.Event() for uid in (alice_id, bob_id)}
    ids = {}
    for uid in (alice_id, bob_id):
        with tenant_scope(uid):
            ids[uid] = TaskWorker.create_task('synthetic-test')
        worker = TaskWorker._worker_for_tenant(uid)
        def make_execution(expected):
            async def execute(cls, *args):
                assert current_tenant_id() == expected
                started[expected].set()
                try:
                    await asyncio.Event().wait()
                finally:
                    assert current_tenant_id() == expected
                    cancelled[expected].set()
            return classmethod(execute)
        monkeypatch.setattr(worker, '_route_task_execution', make_execution(uid))
    with tenant_scope(bob_id), db_session() as conn:
        conn.execute("INSERT INTO tasks(id,task_type,status) VALUES('stale','synthetic-test','processing')")
    TaskWorker.start_all()
    try:
        await asyncio.wait_for(asyncio.gather(*(e.wait() for e in started.values())), 5)
        auth.disable_user('alice')
        await asyncio.wait_for(cancelled[alice_id].wait(), 5)
        assert not cancelled[bob_id].is_set()
        with tenant_scope(bob_id), db_session() as conn:
            assert conn.execute("SELECT status FROM tasks WHERE id='stale'").fetchone()[0] == 'cancelled'
            assert conn.execute("SELECT status FROM tasks WHERE id=?", (ids[bob_id],)).fetchone()[0] == 'processing'
    finally:
        await TaskWorker.stop_all()
    assert cancelled[bob_id].is_set()
    for uid in (alice_id, bob_id):
        with tenant_scope(uid), db_session() as conn:
            assert conn.execute("SELECT status FROM tasks WHERE id=?", (ids[uid],)).fetchone()[0] == 'cancelled'


@pytest.mark.asyncio
async def test_ten_sessions_and_two_simultaneous_uploads(public_env):
    import asyncio
    import httpx
    _, alice_id, bob_id = public_env
    tokens = {}
    for uid, password in [(alice_id, PASSWORD_A), (bob_id, PASSWORD_B)]:
        tokens[uid] = auth.change_password(uid, password, password + '-new')
    async def visit(uid):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN,
                                     cookies={'rag_session': auth.create_session(uid)[0]}) as client:
            response = await client.get('/api/materials')
            assert response.status_code == 200
            assert response.json() == []
    await asyncio.gather(*(visit(alice_id if i % 2 == 0 else bob_id) for i in range(10)))
    async def upload(uid):
        token = tokens[uid]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN,
                                     cookies={'rag_session': token}) as client:
            response = await client.post('/api/materials/upload',
                headers={'Origin': ORIGIN, 'X-CSRF-Token': auth.csrf_token(token)},
                data={'file_type': 'textbook'}, files={'file': ('same.pdf', b'%PDF synthetic queue fixture', 'application/pdf')})
            assert response.status_code == 200, response.text
            assert len((await client.get('/api/materials')).json()) == 1
            return response.json()['id']
    a, b = await asyncio.gather(upload(alice_id), upload(bob_id))
    assert a != b
    # This test checks concurrent HTTP/queue isolation, not document parsing.
    for uid in (alice_id, bob_id):
        with tenant_scope(uid), db_session() as conn:
            assert conn.execute('SELECT COUNT(*) FROM documents').fetchone()[0] == 1
            assert len(list(settings.uploads_path.glob('*.pdf'))) == 1
