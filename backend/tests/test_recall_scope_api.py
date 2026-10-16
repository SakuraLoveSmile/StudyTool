"""Source-driven recall generation HTTP contract, without provider calls."""
import json

from fastapi.testclient import TestClient

from app.database import db_session
from app.main import app


def _seed():
    with db_session() as conn:
        conn.execute("INSERT INTO documents(id,filename,file_hash,file_type,status) VALUES('book','管理学.docx','file-hash','textbook','completed')")
        conn.execute("INSERT INTO document_chunks(id,doc_id,chunk_index,heading,content) VALUES('chunk','book',0,'第一章 管理','管理是协调资源实现目标的过程。')")
        conn.execute("INSERT INTO knowledge_points(id,name,chapter,source_reference) VALUES('point','管理的含义','第一节 管理的概念',?)", (json.dumps({'chunk_ids': ['chunk']}),))


def test_preview_to_atomic_generation_and_replay(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, 'PUBLIC_MODE', False)
    _seed()
    client = TestClient(app)
    preview = client.post('/api/learning/generation-preview', json={'doc_id': 'book'})
    assert preview.status_code == 200, preview.text
    data = preview.json()
    assert data['eligible_count'] == 1
    with db_session() as conn:
        assert conn.execute('SELECT count(*) FROM recall_chapters').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
    payload = {'doc_id': 'book', 'preview_fingerprint': data['fingerprint'], 'request_id': 'api-source-test', 'instructions': '重点比较管理与决策'}
    submitted = client.post('/api/learning/generations/from-source', json=payload)
    assert submitted.status_code == 200, submitted.text
    replay = client.post('/api/learning/generations/from-source', json=payload)
    assert replay.status_code == 200, replay.text
    assert replay.json() == submitted.json()
    with db_session() as conn:
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1
        snapshot = json.loads(conn.execute('SELECT input_snapshot_json FROM recall_generation_batches').fetchone()[0])
        assert snapshot['instructions'] == payload['instructions']
    changed = client.post('/api/learning/generations/from-source', json={**payload, 'instructions': '不同要求'})
    assert changed.status_code in (409, 422), changed.text
    assert changed.json()['detail']['message']


def test_source_generation_requires_preview_and_request_id(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, 'PUBLIC_MODE', False)
    _seed()
    client = TestClient(app)
    for payload in ({'doc_id': 'book'}, {'doc_id': 'book', 'request_id': 'no-preview'}):
        response = client.post('/api/learning/generations/from-source', json=payload)
        assert response.status_code == 422, response.text
        assert response.json()['detail']['message']
    with db_session() as conn:
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0


def test_source_endpoints_keep_session_csrf_and_tenant_boundaries(monkeypatch):
    from fastapi import FastAPI
    from app import auth
    from app.access import AccessMiddleware
    from app.config import settings
    from app.database import init_db
    from app.routers.learning import router
    from app.tenancy import tenant_scope

    owner = '00000000-0000-4000-8000-000000000001'
    stranger = '00000000-0000-4000-8000-000000000002'
    for user in (owner, stranger):
        with tenant_scope(user):
            init_db()
    with tenant_scope(owner):
        _seed()
    monkeypatch.setattr(settings, 'PUBLIC_MODE', True)
    monkeypatch.setattr(settings, 'PUBLIC_ORIGIN', 'https://study.example.com')
    monkeypatch.setattr(auth, 'authenticate_session', lambda token: {
        'id': owner if token == 'owner' else stranger,
        'must_change_password': False,
    } if token in ('owner', 'stranger') else None)
    isolated = FastAPI()
    isolated.add_middleware(AccessMiddleware)
    isolated.include_router(router)
    client = TestClient(isolated)
    headers = {'Origin': settings.PUBLIC_ORIGIN}
    assert client.post('/api/learning/generation-preview', json={'doc_id': 'book'}, headers=headers).status_code == 401
    client.cookies.set('rag_session', 'owner')
    assert client.post('/api/learning/generation-preview', json={'doc_id': 'book'}, headers=headers).status_code == 403
    headers['X-CSRF-Token'] = auth.csrf_token('owner')
    result = client.post('/api/learning/generation-preview', json={'doc_id': 'book'}, headers=headers)
    assert result.status_code == 200, result.text
    client.cookies.set('rag_session', 'stranger')
    headers['X-CSRF-Token'] = auth.csrf_token('stranger')
    assert client.post('/api/learning/generation-preview', json={'doc_id': 'book'}, headers=headers).status_code == 404
    result = client.post('/api/learning/generations/from-source', json={
        'doc_id': 'book', 'request_id': 'stranger-request', 'preview_fingerprint': result.json()['fingerprint'],
    }, headers=headers)
    assert result.status_code == 404, result.text
    with tenant_scope(owner), db_session() as conn:
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
