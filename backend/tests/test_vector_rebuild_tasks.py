"""Tenant-local index recovery: fake vectors and isolated SQLite, no paid calls."""
import asyncio

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.database import db_session, init_db, is_vector_locked, get_system_metadata
from app.desktop import desktop_router, MaintenanceManager, enqueue_reindex, list_reindex_tasks
from app.routers.materials import get_task_status, get_task_events
from app.services.task_worker import TaskWorker
from app.services.retrieval.embedding import EmbeddingService
from app.tenancy import tenant_scope


@pytest.fixture(autouse=True)
def maintenance():
    MaintenanceManager.force_clear()
    yield
    MaintenanceManager.force_clear()


def seed(invalid=False):
    with db_session() as conn:
        conn.execute("INSERT INTO documents(id,filename,file_hash,file_type,status) VALUES('doc','book.docx','hash','textbook','completed')")
        for i in range(12):
            conn.execute("""INSERT INTO document_chunks(id,doc_id,chunk_index,content,embedding,embedding_status)
                VALUES(?, 'doc', ?, ?, X'AABBCC', 'completed')""", (str(i), i, f'会计正文 {i}'))
        if invalid:
            conn.execute("UPDATE document_chunks SET content='',parse_error='OCR failed' WHERE id='11'")
    assert is_vector_locked()[0]


def before():
    with db_session() as conn:
        return [tuple(r) for r in conn.execute('SELECT id,content,embedding FROM document_chunks ORDER BY id')]


def task_dict(task):
    return {'id':task.id,'task_type':task.task_type,'target_id':task.target_id}


def test_submit_fast_deduplicates_and_restores_without_provider(monkeypatch):
    seed()
    async def forbidden(*args, **kwargs):
        pytest.fail('Submission must never call the provider')
    monkeypatch.setattr(EmbeddingService, 'get_embeddings_batch', forbidden)
    app = FastAPI(); app.include_router(desktop_router)
    client = TestClient(app)
    first = client.post('/api/desktop/reindex/tasks')
    assert first.status_code == 202
    assert first.json()['status'] == 'pending'
    assert client.post('/api/desktop/reindex/tasks').json()['id'] == first.json()['id']
    assert client.get('/api/desktop/reindex/tasks').json()[0]['id'] == first.json()['id']
    assert is_vector_locked()[0]


@pytest.mark.asyncio
async def test_success_atomic_unlock_body_preserved_and_logged(monkeypatch):
    seed(invalid=True)
    original = before()
    calls=[]
    async def vectors(texts):
        calls.append(list(texts)); assert is_vector_locked()[0]
        assert before() == original
        return [[1,2,3] for _ in texts]
    monkeypatch.setattr(EmbeddingService,'get_embeddings_batch',vectors)
    task=enqueue_reindex()
    await TaskWorker._dispatch_task(task_dict(task))
    status=get_task_status(task.id)
    assert status.status=='completed' and status.progress==100
    assert status.progress_detail.succeeded==11
    assert len(calls)==2 and sum(map(len,calls))==11
    assert not is_vector_locked()[0]
    assert [(r[0],r[1]) for r in before()]==[(r[0],r[1]) for r in original]
    with db_session() as conn:
        assert conn.execute("SELECT embedding FROM document_chunks WHERE id='11'").fetchone()[0] is None
        row=conn.execute("SELECT embedding FROM document_chunks WHERE id='0'").fetchone()
        assert np.allclose(EmbeddingService.deserialize_embedding(row[0]), [1,2,3])
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'vector_rebuild_%'").fetchone()[0]==0
    assert get_system_metadata('embedding_model')=='mock'
    events=get_task_events(task.id).events
    assert {e.stage for e in events}>={'read_chunks','generate_embeddings','save_index'}
    assert any(e.event_type=='completed' for e in events)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['provider','dimension','changed','cancel','config'])
async def test_rebuild_failure_preserves_old_index_and_lock(monkeypatch, failure):
    seed(); original=before(); count=0; task=enqueue_reindex()
    async def vectors(texts):
        nonlocal count
        count+=1
        if failure=='provider' and count==2:
            raise RuntimeError('sk-secret provider-body 原文')
        if failure=='changed':
            with db_session() as conn:
                conn.execute("UPDATE document_chunks SET content='用户最新校对' WHERE id='0'")
        if failure=='cancel':
            TaskWorker.request_cancel(task.id)
        if failure=='config':
            monkeypatch.setattr(settings,'EMBEDDING_MODE','remote')
            monkeypatch.setattr(settings,'EMBEDDING_MODEL','another-model')
        return [[1,2] if failure=='dimension' and count==2 else [1,2,3] for _ in texts]
    monkeypatch.setattr(EmbeddingService,'get_embeddings_batch',vectors)
    await TaskWorker._dispatch_task(task_dict(task))
    status=get_task_status(task.id)
    assert status.status==('cancelled' if failure=='cancel' else 'failed')
    assert is_vector_locked()[0]
    assert [r[2] for r in before()]==[r[2] for r in original]
    if failure!='changed': assert before()==original
    else: assert next(r for r in before() if r[0]=='0')[1]=='用户最新校对'
    assert 'secret' not in (status.error_msg or '')
    with db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'vector_rebuild_%'").fetchone()[0]==0


@pytest.mark.asyncio
async def test_queued_config_change_rejected_before_billing(monkeypatch):
    seed(); task=enqueue_reindex()
    monkeypatch.setattr(settings,'EMBEDDING_MODE','remote')
    monkeypatch.setattr(settings,'EMBEDDING_API_KEY','fake')
    async def forbidden(*args): pytest.fail('Changed config requires user confirmation')
    monkeypatch.setattr(EmbeddingService,'get_embeddings_batch',forbidden)
    await TaskWorker._dispatch_task(task_dict(task))
    assert get_task_status(task.id).status=='failed'
    assert '配置已变化' in get_task_status(task.id).error_msg


@pytest.mark.asyncio
async def test_tenant_scope_and_no_usable_chunks(monkeypatch):
    monkeypatch.setattr(settings, 'PUBLIC_MODE', True)
    with tenant_scope('11111111-1111-4111-8111-111111111111'):
        init_db(); seed(invalid=True)
        with db_session() as conn:
            conn.execute("UPDATE document_chunks SET content='' ")
        a_before=before(); task=enqueue_reindex()
    with tenant_scope('22222222-2222-4222-8222-222222222222'):
        init_db(); seed(); b_before=before()
        assert list_reindex_tasks()==[]
    async def forbidden(*args): pytest.fail('No usable text must not be billed')
    monkeypatch.setattr(EmbeddingService,'get_embeddings_batch',forbidden)
    with tenant_scope('11111111-1111-4111-8111-111111111111'):
        await TaskWorker._dispatch_task(task_dict(task))
        assert get_task_status(task.id).status=='completed'
        assert not is_vector_locked()[0]
        assert all(r[2] is None for r in before())
        assert [r[1] for r in before()]==[r[1] for r in a_before]
    with tenant_scope('22222222-2222-4222-8222-222222222222'):
        assert before()==b_before and is_vector_locked()[0]
        assert list_reindex_tasks()==[]


@pytest.mark.asyncio
async def test_rebuild_recovers_document_readiness_without_reocr(monkeypatch):
    from app.services import library
    seed()
    with db_session() as conn:
        conn.execute("UPDATE documents SET status='failed',error_msg='向量索引已锁定: 来源缺失'")
    async def vectors(texts): return [[1,2,3] for _ in texts]
    monkeypatch.setattr(EmbeddingService,'get_embeddings_batch',vectors)
    task=enqueue_reindex()
    await TaskWorker._dispatch_task(task_dict(task))
    with db_session() as conn:
        doc=conn.execute("SELECT status,error_msg FROM documents").fetchone()
        assert tuple(doc)==('completed',None)
    assert library.list_chapters('doc')


def test_generic_retry_requires_new_cost_confirmation():
    from fastapi import HTTPException
    from app.routers.materials import retry_task
    task=enqueue_reindex()
    TaskWorker.mark_task_failed(task.id,'synthetic provider failure')
    with pytest.raises(HTTPException) as error:
        retry_task(task.id)
    assert error.value.status_code==409 and '确认费用' in error.value.detail
    assert len(list_reindex_tasks())==1


def test_public_rebuild_requires_session_csrf_and_lists_only_owner(monkeypatch):
    from app import auth
    from app.access import AccessMiddleware
    monkeypatch.setattr(settings, 'PUBLIC_MODE', True)
    monkeypatch.setattr(settings, 'PUBLIC_ORIGIN', 'https://testserver')
    auth.init_auth_db()
    alice=auth.provision_user('alice-vector','synthetic-pass-123')
    bob=auth.provision_user('bob-vector','synthetic-pass-123')
    with auth._db() as conn:
        conn.execute('UPDATE users SET must_change_password=0')
    app=FastAPI(); app.include_router(desktop_router); app.add_middleware(AccessMiddleware)
    client=TestClient(app,base_url='https://testserver')
    assert client.get('/api/desktop/reindex/tasks').status_code==401
    token,csrf=auth.create_session(alice)
    client.cookies.set('rag_session',token)
    assert client.post('/api/desktop/reindex/tasks',headers={'Origin':'https://testserver'}).status_code==403
    first=client.post('/api/desktop/reindex/tasks',headers={'Origin':'https://testserver','X-CSRF-Token':csrf})
    assert first.status_code==202
    assert client.post('/api/desktop/reindex/tasks',headers={'Origin':'https://evil.invalid','X-CSRF-Token':csrf}).status_code==403
    token,_=auth.create_session(bob); client.cookies.set('rag_session',token)
    assert client.get('/api/desktop/reindex/tasks').json()==[]
