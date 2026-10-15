"""Vector provenance regressions; temporary databases and synthetic providers only."""
from unittest.mock import AsyncMock

import httpx
import numpy as np
import pymupdf as fitz
import pytest

from app.config import settings
from app.database import (
    assert_embedding_source, current_embedding_source, db_session,
    get_system_metadata, is_vector_locked, record_embedding_source,
    set_system_metadata,
)
from app.services.http_client import HttpClientProvider
from app.services.parser.pdf_parser import PDFParser
from app.services.retrieval.embedding import EmbeddingService
from app.services.task_worker import TaskWorker


@pytest.fixture(autouse=True)
def reset_embedding_caches(monkeypatch):
    monkeypatch.setattr(EmbeddingService, "_vector_cache", {})
    monkeypatch.setattr(EmbeddingService, "_compatibility_cache", None)
    monkeypatch.setattr(EmbeddingService, "_compatibility_source", None)


def insert_chunk(*, embedding=None, chunk_id="chunk-1"):
    with db_session() as conn:
        conn.execute("INSERT OR IGNORE INTO documents(id,filename,file_hash,file_type,status) "
                     "VALUES('doc-1','book.pdf','synthetic-book','textbook','pending')")
        conn.execute("INSERT INTO document_chunks(id,doc_id,chunk_index,content,embedding,embedding_status) "
                     "VALUES(?,'doc-1',?,'会计信息支持管理决策',?,'pending')",
                     (chunk_id, 0 if chunk_id == "chunk-1" else 1, embedding))


def remote_config(monkeypatch, url="https://provider-a.example/v1"):
    monkeypatch.setattr(settings, "EMBEDDING_MODE", "remote")
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "accounting-model")
    monkeypatch.setattr(settings, "EMBEDDING_BASE_URL", url)
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "synthetic-test-key")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["mock", "remote"])
async def test_first_single_vector_records_source_and_second_can_append(monkeypatch, mode):
    if mode == "remote":
        remote_config(monkeypatch)
        monkeypatch.setattr(EmbeddingService, "get_embedding", AsyncMock(return_value=np.ones(3, dtype=np.float32)))
    insert_chunk()
    first = TaskWorker.create_task("reindex_chunk", "chunk-1")
    await TaskWorker._execute_reindex_chunk(first, "chunk-1")
    assert get_system_metadata("embedding_mode") == mode
    assert get_system_metadata("embedding_model") == ("mock" if mode == "mock" else "accounting-model")
    assert get_system_metadata("embedding_base_url") == ("" if mode == "mock" else "https://provider-a.example/v1")
    assert is_vector_locked() == (False, None)
    insert_chunk(chunk_id="chunk-2")
    second = TaskWorker.create_task("reindex_chunk", "chunk-2")
    await TaskWorker._execute_reindex_chunk(second, "chunk-2")
    with db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM document_chunks WHERE embedding IS NOT NULL").fetchone()[0] == 2
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (second,)).fetchone()[0] == "completed"


@pytest.mark.asyncio
async def test_document_batch_records_source_atomically(monkeypatch):
    insert_chunk()
    with fitz.open() as pdf:
        pdf.new_page()
        pdf.new_page()
        pdf.save(settings.uploads_path / "synthetic-book.pdf")
    monkeypatch.setattr(PDFParser, "parse_pdf", AsyncMock(return_value=[
        {"content": "第一章 会计信息支持管理决策", "page_number": 1},
        {"content": "第二章 预算是计划与控制的工具", "page_number": 2},
    ]))
    task = TaskWorker.create_task("parse_document", "doc-1")
    await TaskWorker._execute_parse_document(task, "doc-1")
    with db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM document_chunks WHERE embedding IS NOT NULL").fetchone()[0] == 2
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (task,)).fetchone()[0] == "completed"
    assert get_system_metadata("embedding_mode") == "mock"
    assert is_vector_locked() == (False, None)


@pytest.mark.asyncio
async def test_unknown_old_vectors_are_preserved_and_provider_not_called(monkeypatch):
    insert_chunk(embedding=b"old-unknown-vector")
    insert_chunk(chunk_id="chunk-2")
    provider = AsyncMock()
    monkeypatch.setattr(EmbeddingService, "get_embedding", provider)
    task = TaskWorker.create_task("reindex_chunk", "chunk-2")
    await TaskWorker._execute_reindex_chunk(task, "chunk-2")
    provider.assert_not_awaited()
    assert is_vector_locked()[0]
    assert get_system_metadata("embedding_model") is None
    with db_session() as conn:
        assert conn.execute("SELECT embedding FROM document_chunks WHERE id='chunk-1'").fetchone()[0] == b"old-unknown-vector"
        assert conn.execute("SELECT embedding FROM document_chunks WHERE id='chunk-2'").fetchone()[0] is None


@pytest.mark.asyncio
async def test_inflight_config_change_never_stamps_or_writes_vector(monkeypatch):
    insert_chunk()
    remote_config(monkeypatch)

    async def fake_embedding(_text):
        monkeypatch.setattr(settings, "EMBEDDING_BASE_URL", "https://provider-b.example/v1")
        return np.ones(3, dtype=np.float32)

    monkeypatch.setattr(EmbeddingService, "get_embedding", fake_embedding)
    task = TaskWorker.create_task("reindex_chunk", "chunk-1")
    await TaskWorker._execute_reindex_chunk(task, "chunk-1")
    with db_session() as conn:
        assert conn.execute("SELECT embedding FROM document_chunks").fetchone()[0] is None
        row = conn.execute("SELECT status,error_msg FROM tasks WHERE id=?", (task,)).fetchone()
        assert row[0] == "failed"
        assert "配置" in row[1]
    assert get_system_metadata("embedding_model") is None



@pytest.mark.asyncio
async def test_unknown_vector_created_while_request_is_inflight_blocks_commit(monkeypatch):
    insert_chunk()

    async def fake_embedding(_text):
        insert_chunk(embedding=b"concurrent-unknown", chunk_id="chunk-2")
        return np.ones(3, dtype=np.float32)

    monkeypatch.setattr(EmbeddingService, "get_embedding", fake_embedding)
    task = TaskWorker.create_task("reindex_chunk", "chunk-1")
    await TaskWorker._execute_reindex_chunk(task, "chunk-1")
    with db_session() as conn:
        assert conn.execute("SELECT embedding FROM document_chunks WHERE id='chunk-1'").fetchone()[0] is None
        assert conn.execute("SELECT embedding FROM document_chunks WHERE id='chunk-2'").fetchone()[0] == b"concurrent-unknown"
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (task,)).fetchone()[0] == "failed"
    assert get_system_metadata("embedding_model") is None
    assert is_vector_locked()[0]

def test_source_record_and_vector_roll_back_together():
    insert_chunk()
    source = current_embedding_source()
    with pytest.raises(RuntimeError, match="synthetic rollback"):
        with db_session() as conn:
            assert_embedding_source(conn, source)
            conn.execute("UPDATE document_chunks SET embedding=?", (b"new-vector",))
            record_embedding_source(conn, source)
            raise RuntimeError("synthetic rollback")
    assert get_system_metadata("embedding_model") is None
    with db_session() as conn:
        assert conn.execute("SELECT embedding FROM document_chunks").fetchone()[0] is None



@pytest.mark.asyncio
async def test_writer_rolls_back_vector_when_provenance_commit_fails(monkeypatch):
    insert_chunk()

    def failing_record(conn, source):
        record_embedding_source(conn, source)
        raise RuntimeError("synthetic metadata failure")

    monkeypatch.setattr("app.services.task_worker.record_embedding_source", failing_record)
    task = TaskWorker.create_task("reindex_chunk", "chunk-1")
    await TaskWorker._execute_reindex_chunk(task, "chunk-1")
    with db_session() as conn:
        assert conn.execute("SELECT embedding FROM document_chunks").fetchone()[0] is None
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (task,)).fetchone()[0] == "failed"
    assert get_system_metadata("embedding_model") is None

@pytest.mark.parametrize("change", ["mode", "model", "endpoint"])
def test_existing_source_mismatch_blocks_append(monkeypatch, change):
    remote_config(monkeypatch)
    source = current_embedding_source()
    insert_chunk(embedding=b"known-vector")
    with db_session() as conn:
        record_embedding_source(conn, source)
    if change == "mode":
        monkeypatch.setattr(settings, "EMBEDDING_MODE", "mock")
    elif change == "model":
        monkeypatch.setattr(settings, "EMBEDDING_MODEL", "other-model")
    else:
        monkeypatch.setattr(settings, "EMBEDDING_BASE_URL", "https://provider-b.example/v1")
    assert is_vector_locked()[0]
    with pytest.raises(RuntimeError, match="锁定"):
        with db_session() as conn:
            assert_embedding_source(conn, current_embedding_source())


def test_complete_legacy_remote_provenance_is_compatible(monkeypatch):
    remote_config(monkeypatch)
    insert_chunk(embedding=b"legacy-vector")
    set_system_metadata("embedding_model", settings.EMBEDDING_MODEL)
    set_system_metadata("embedding_base_url", settings.EMBEDDING_BASE_URL + "/")
    assert is_vector_locked() == (False, None)
    with db_session() as conn:
        assert_embedding_source(conn, current_embedding_source())


def test_effective_url_fallback_is_part_of_source(monkeypatch):
    remote_config(monkeypatch, "")
    monkeypatch.setattr(settings, "LLM_BASE_URL", "https://fallback.example/v1/")
    assert current_embedding_source().base_url == "https://fallback.example/v1"


@pytest.mark.asyncio
async def test_endpoint_change_invalidates_vector_and_compatibility_caches(monkeypatch):
    remote_config(monkeypatch)
    monkeypatch.setattr(settings, "EMBEDDING_MODE", " REMOTE ")
    requests = []

    async def respond(request):
        requests.append(request)
        vector = [1.0, 0.0] if request.url.host == "provider-a.example" else [0.0, 1.0, 0.0]
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": vector}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False) as client:
        monkeypatch.setattr(HttpClientProvider, "_client", client)
        a = await EmbeddingService.get_embeddings_batch(["same source text"])
        monkeypatch.setattr(settings, "EMBEDDING_BASE_URL", "https://provider-b.example/v1")
        b = await EmbeddingService.get_embeddings_batch(["same source text"])
    assert len(a[0]) == 2 and len(b[0]) == 3
    assert [r.url.host for r in requests] == ["provider-a.example", "provider-a.example", "provider-b.example", "provider-b.example"]


@pytest.mark.asyncio
async def test_remote_response_after_config_change_not_cached(monkeypatch):
    remote_config(monkeypatch)

    async def respond(request):
        monkeypatch.setattr(settings, "EMBEDDING_BASE_URL", "https://provider-b.example/v1")
        return httpx.Response(200, json={"data": [{"embedding": [1.0, 0.0]}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False) as client:
        monkeypatch.setattr(HttpClientProvider, "_client", client)
        with pytest.raises(RuntimeError, match="配置"):
            await EmbeddingService.get_embedding("changed while requesting")
    assert EmbeddingService._vector_cache == {}
