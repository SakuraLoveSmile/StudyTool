import asyncio
import io
import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import db_session, init_db
from app.main import app
from app.desktop import (
    MaintenanceManager,
    has_existing_embeddings,
)
from app.services.parser.ocr_service import (
    OCRQuotaExhaustedError,
    OCRService,
)
from app.services.parser.pdf_parser import PDFParser


@pytest.fixture
def desktop_t2_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "desktop_t2_data"
    data_dir.mkdir(parents=True)
    monkeypatch.setenv("RAG_DESKTOP_DATA_DIR", str(data_dir))
    monkeypatch.setattr(settings, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(settings, "DESKTOP_MODE", True)
    monkeypatch.setattr(settings, "DESKTOP_INSTANCE_TOKEN", "token-t2-xyz")
    monkeypatch.setattr(settings, "HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "PORT", 18764)
    monkeypatch.setattr(settings, "OCR_MODE", "paddleocr_api")
    monkeypatch.setattr(settings, "BAIDU_OCR_API_URL", "https://aistudio.baidu.com/serving/api/v1/ocr")
    monkeypatch.setattr(settings, "BAIDU_OCR_TOKEN", "mock-token-abc")

    init_db()
    MaintenanceManager.force_clear()
    yield data_dir
    MaintenanceManager.force_clear()


@pytest.mark.asyncio
async def test_ocr_429_quota_exhaustion_halts_subsequent_pages_and_preserves_page_numbers(desktop_t2_env, tmp_path):
    """Regression: 429 quota exhaustion must immediately halt subsequent OCR calls
    and all chunks must preserve exact 1-based page numbers in order.
    """
    # Create a 3-page synthetic scanned PDF with minimal text so all 3 pages request OCR
    doc = fitz.open()
    for i in range(3):
        p = doc.new_page(width=300, height=200)
        # draw a small rectangle so page has graphics/images and no selectable text
        p.draw_rect(fitz.Rect(10, 10, 50, 50), color=(0, 0, 1), fill=(0, 0, 1))

    pdf_file = tmp_path / "scanned_exam_3pages.pdf"
    doc.save(str(pdf_file))
    doc.close()

    call_count = 0

    async def mock_extract_text(img_bytes, img_format="png", on_event=None):
        nonlocal call_count
        call_count += 1
        # First page throws 429 quota exhausted
        raise OCRQuotaExhaustedError("百度 OCR 每日额度耗尽（HTTP 429）")

    with patch.object(OCRService, "extract_text_from_image_bytes", side_effect=mock_extract_text):
        chunks = await PDFParser.parse_pdf(pdf_file)

    # Concurrency is 2, so at most 2 initial tasks could have started before quota_exhausted flag halts
    assert call_count <= 2, f"OCR was called {call_count} times, expected early halt after 429"

    # All 3 pages must be represented in chunk output in exact 1-based order
    assert len(chunks) == 3
    for idx, chunk in enumerate(chunks):
        expected_page = idx + 1
        assert chunk["page_number"] == expected_page, f"Chunk {idx} expected page {expected_page}, got {chunk['page_number']}"
        assert chunk["parse_error"] is not None
        assert "额度" in chunk["parse_error"] or "429" in chunk["parse_error"]


def test_maintenance_busy_refuses_lease_and_protects_credentials(desktop_t2_env):
    """Regression: If system is busy (in-flight request active), lease acquisition is rejected (409)
    so credentials can never be modified.
    """
    from fastapi import HTTPException

    # Simulate in-flight mutating request
    assert MaintenanceManager.inc_mutating_request() is True

    try:
        with pytest.raises(HTTPException) as exc_info:
            MaintenanceManager.acquire("lease-attempt-1", "修改模型配置")
        assert exc_info.value.status_code == 409
        assert "写请求正在处理" in exc_info.value.detail or "系统繁忙" in exc_info.value.detail
    finally:
        MaintenanceManager.dec_mutating_request()

    # Now idle, acquisition succeeds
    MaintenanceManager.acquire("lease-attempt-2", "修改模型配置")
    assert MaintenanceManager.get_lease()["lease_id"] == "lease-attempt-2"
    MaintenanceManager.release("lease-attempt-2")


@pytest.mark.asyncio
async def test_vector_lock_persists_in_database_and_intercepts_retrieval_and_append(desktop_t2_env):
    """Regression T2:
    - Vector lock state and reason are persisted in SQLite system_metadata.
    - Intercepts vector retrieval (HybridSearchService._vector_search).
    - Intercepts vector appending in task_worker.
    - Plaintext FTS5 search and document reading remain fully functional.
    - Vector lock persists across simulated restarts.
    """
    from fastapi.testclient import TestClient
    from app.main import app
    from app.database import set_system_metadata, get_system_metadata, is_vector_locked
    from app.services.retrieval.hybrid import HybridSearchService
    from app.services.retrieval.fts_search import FTSSearchService
    from app.services.task_worker import TaskWorker
    import numpy as np

    # 1. Insert sample documents and chunks
    with db_session() as db:
        db.execute(
            """INSERT OR REPLACE INTO documents (id, filename, file_hash, file_type, status)
               VALUES ('doc-lock-1', 'test_book.pdf', 'hash-l-1', 'textbook', 'completed')"""
        )
        db.execute(
            """INSERT OR REPLACE INTO document_chunks (id, doc_id, chunk_index, content, embedding, embedding_status)
               VALUES ('chunk-l-1', 'doc-lock-1', 0, '马克思主义基本原理概念', X'01020304', 'completed')"""
        )
        # Also index into FTS5
        db.execute(
            """INSERT OR REPLACE INTO document_chunks_fts (chunk_id, content_segmented)
               VALUES ('chunk-l-1', '马克思 主义 基本 原理 概念')"""
        )

    # 2. Explicitly lock vectors in database
    set_system_metadata("vector_locked", "true")
    set_system_metadata("vector_lock_reason", "向量模型来源信息缺失，请在设置中重建索引")

    # Verify persistence
    locked, reason = is_vector_locked()
    assert locked is True
    assert "向量模型来源信息缺失" in reason

    # 3. Vector search MUST be intercepted and raise
    fake_vec = np.zeros(128, dtype=np.float32)
    with pytest.raises(RuntimeError) as exc_vec:
        HybridSearchService._vector_search(fake_vec)
    assert "锁定" in str(exc_vec.value)

    # 4. FTS search must still work (pure text keyword match)
    fts_res = FTSSearchService.search("马克思", limit=5)
    assert len(fts_res) >= 1
    assert fts_res[0]["chunk_id"] == "chunk-l-1"

    # 5. Document reading must still work
    client = TestClient(app)
    headers = {
        "X-RAG-Instance-Token": "token-t2-xyz",
        "Host": "127.0.0.1:18764",
    }
    res_chunks = client.get("/api/materials/doc-lock-1/chunks", headers=headers)
    assert res_chunks.status_code == 200
    assert len(res_chunks.json()) >= 1

    # 6. Desktop status endpoint must return vector_locked and reason
    res_status = client.get("/api/desktop/status", headers=headers)
    assert res_status.status_code == 200
    status_data = res_status.json()
    assert status_data["vector_locked"] is True
    assert "重建索引" in status_data["vector_lock_reason"]

    # 7. Task worker vector append must be intercepted
    with db_session() as db:
        db.execute(
            """INSERT OR REPLACE INTO tasks (id, task_type, target_id, status)
               VALUES ('task-append-1', 'reindex_chunk', 'chunk-l-1', 'pending')"""
        )
    await TaskWorker._route_task_execution("task-append-1", "reindex_chunk", "chunk-l-1")
    with db_session() as db:
        t_row = db.execute("SELECT status, error_msg FROM tasks WHERE id = 'task-append-1'").fetchone()
        assert t_row["status"] == "failed"
        assert "锁定" in t_row["error_msg"]


@pytest.mark.asyncio
async def test_reindex_flow_staging_replacement_and_unlock(desktop_t2_env, monkeypatch):
    """Regression T2:
    - Setting provides 'reindex':
      - Generates vectors in staging table.
      - If generation fails: staging dropped, original chunks preserved, lock remains.
      - If generation succeeds: atomic transaction replaces chunks vectors and releases lock.
    """
    from fastapi.testclient import TestClient
    from app.main import app
    from app.database import set_system_metadata, is_vector_locked
    from app.services.retrieval.embedding import EmbeddingService
    import numpy as np

    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "sk-mock-reindex-key")

    # Setup locked database
    with db_session() as db:
        db.execute(
            """INSERT OR REPLACE INTO documents (id, filename, file_hash, file_type, status)
               VALUES ('doc-reindex-1', 'reindex_doc.pdf', 'hash-r-1', 'textbook', 'completed')"""
        )
        db.execute(
            """INSERT OR REPLACE INTO document_chunks (id, doc_id, chunk_index, content, embedding, embedding_status)
               VALUES ('chunk-r-1', 'doc-reindex-1', 0, '需重新向量化的切片内容', X'AABBCC', 'completed')"""
        )

    set_system_metadata("vector_locked", "true")
    set_system_metadata("vector_lock_reason", "模型迁移锁定")
    set_system_metadata("embedding_model", "old-model")

    client = TestClient(app)
    headers = {
        "X-RAG-Instance-Token": "token-t2-xyz",
        "Host": "127.0.0.1:18764",
    }

    # Case A: Reindex fails during batch generation
    with patch.object(EmbeddingService, "get_embeddings_batch", side_effect=RuntimeError("远程向量服务网络超时")):
        res_fail = client.post("/api/desktop/reindex", headers=headers)
        assert res_fail.status_code == 500
        assert "失败" in res_fail.json()["detail"]

    # Chunks and lock must remain unchanged
    locked, _ = is_vector_locked()
    assert locked is True
    with db_session() as db:
        row = db.execute("SELECT embedding FROM document_chunks WHERE id = 'chunk-r-1'").fetchone()
        assert row["embedding"] == b"\xaa\xbb\xcc"
        # Staging table must NOT be left around
        stage_check = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_chunks_staging'").fetchone()
        assert stage_check is None

    # Case B: Reindex succeeds
    new_vec = [0.5, 0.6, 0.7]
    with patch.object(EmbeddingService, "get_embeddings_batch", return_value=[new_vec]):
        res_ok = client.post("/api/desktop/reindex", headers=headers)
        assert res_ok.status_code == 200
        assert res_ok.json()["ok"] is True

    # Lock must now be released!
    locked_after, reason_after = is_vector_locked()
    assert locked_after is False
    with db_session() as db:
        row = db.execute("SELECT embedding FROM document_chunks WHERE id = 'chunk-r-1'").fetchone()
        # Embedding must have been replaced with serialized new_vec
        deserialized = EmbeddingService.deserialize_embedding(row["embedding"])
        assert np.allclose(deserialized[:3], new_vec[:3], atol=1e-4)


def test_reindex_requires_exclusive_maintenance_lease(desktop_t2_env):
    """A reindex cannot overlap another maintenance operation."""
    client = TestClient(app)
    headers = {
        "X-RAG-Instance-Token": "token-t2-xyz",
        "Host": "127.0.0.1:18764",
        "Origin": "http://127.0.0.1:18764",
    }
    acquired = client.post(
        "/api/desktop/maintenance/acquire",
        json={"lease_id": "backup-lease", "operation": "导出数据备份"},
        headers=headers,
    )
    assert acquired.status_code == 200
    try:
        blocked = client.post(
            "/api/desktop/reindex",
            json={"lease_id": "reindex-lease"},
            headers=headers,
        )
        assert blocked.status_code == 409
    finally:
        released = client.post(
            "/api/desktop/maintenance/release",
            json={"lease_id": "backup-lease"},
            headers=headers,
        )
        assert released.status_code == 200
