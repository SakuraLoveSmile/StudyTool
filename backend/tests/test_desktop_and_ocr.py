import asyncio
import base64
import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import db_session, init_db
from app.desktop import (
    DesktopAccessMiddleware,
    check_system_idle,
    has_existing_embeddings,
)
from app.main import app
from app.services.parser.ocr_service import (
    OCRQuotaExhaustedError,
    OCRService,
    OCRServiceError,
)
from app.services.parser.pdf_parser import PDFParser


@pytest.fixture
def isolated_desktop_env(tmp_path, monkeypatch):
    """Setup an isolated desktop environment for tests."""
    data_dir = tmp_path / "desktop_data"
    data_dir.mkdir(parents=True)
    monkeypatch.setenv("RAG_DESKTOP_DATA_DIR", str(data_dir))
    monkeypatch.setattr(settings, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(settings, "DESKTOP_MODE", True)
    monkeypatch.setattr(settings, "DESKTOP_INSTANCE_TOKEN", "test-secret-token-12345")
    monkeypatch.setattr(settings, "HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "PORT", 18764)
    monkeypatch.setattr(settings, "PUBLIC_MODE", False)

    init_db()
    yield data_dir


def test_desktop_access_middleware_enforces_token_and_host(isolated_desktop_env):
    client = TestClient(app)

    # 1. /health should succeed without instance token
    res_health = client.get("/health")
    assert res_health.status_code == 200
    assert res_health.json()["status"] == "ok"

    # 2. /api/desktop/status without token should be rejected (401)
    res_no_token = client.get("/api/desktop/status")
    assert res_no_token.status_code == 401
    assert res_no_token.json()["detail"]["code"] == "DESKTOP_AUTH_REQUIRED"

    # 3. /api/desktop/status with invalid token should be rejected (401)
    res_bad_token = client.get(
        "/api/desktop/status",
        headers={"X-RAG-Instance-Token": "wrong-token", "Host": "127.0.0.1:18764"},
    )
    assert res_bad_token.status_code == 401

    # 4. /api/desktop/status with invalid host should be rejected (403)
    res_bad_host = client.get(
        "/api/desktop/status",
        headers={"X-RAG-Instance-Token": "test-secret-token-12345", "Host": "attacker.com:18764"},
    )
    assert res_bad_host.status_code == 403
    assert res_bad_host.json()["detail"]["code"] == "INVALID_HOST"

    # 5. /api/desktop/status with valid token and host should succeed
    res_valid = client.get(
        "/api/desktop/status",
        headers={"X-RAG-Instance-Token": "test-secret-token-12345", "Host": "127.0.0.1:18764"},
    )
    assert res_valid.status_code == 200
    data = res_valid.json()
    assert data["desktop_mode"] is True
    assert data["idle"] is True


def test_desktop_access_middleware_rejects_cross_origin_writes(isolated_desktop_env):
    client = TestClient(app)

    # Disallowed external origin on mutating POST
    res_bad_origin = client.post(
        "/api/desktop/config/validate",
        json={"new_embedding_model": "test"},
        headers={
            "X-RAG-Instance-Token": "test-secret-token-12345",
            "Host": "127.0.0.1:18764",
            "Origin": "http://evil-site.com",
        },
    )
    assert res_bad_origin.status_code == 403
    assert res_bad_origin.json()["detail"]["code"] == "ORIGIN_REJECTED"

    # Allowed local origin
    res_good_origin = client.post(
        "/api/desktop/config/validate",
        json={"new_embedding_model": settings.EMBEDDING_MODEL},
        headers={
            "X-RAG-Instance-Token": "test-secret-token-12345",
            "Host": "127.0.0.1:18764",
            "Origin": "http://127.0.0.1:18764",
        },
    )
    assert res_good_origin.status_code == 200
    assert res_good_origin.json()["allowed"] is True


def test_embedding_lock_when_vectors_exist(isolated_desktop_env):
    client = TestClient(app)
    headers = {
        "X-RAG-Instance-Token": "test-secret-token-12345",
        "Host": "127.0.0.1:18764",
        "Origin": "http://127.0.0.1:18764",
    }

    # Before embeddings exist: modifying model is allowed
    assert has_existing_embeddings() is False
    res = client.post("/api/desktop/config/validate", json={"new_embedding_model": "other-model"}, headers=headers)
    assert res.status_code == 200

    # Insert a dummy document and chunk with non-null embedding
    with db_session() as db:
        db.execute(
            """INSERT INTO documents (id, filename, file_hash, file_type, status)
               VALUES ('d1', 'test.pdf', 'hash-12345', 'textbook', 'completed')"""
        )
        db.execute(
            """INSERT INTO document_chunks (doc_id, chunk_index, content, embedding, embedding_status)
               VALUES ('d1', 0, 'test content', X'00010203', 'completed')"""
        )

    # Now embeddings exist
    assert has_existing_embeddings() is True

    # Attempting to change embedding model or base url must be rejected (400)
    res_change_model = client.post(
        "/api/desktop/config/validate",
        json={"new_embedding_model": "new-incompatible-model"},
        headers=headers,
    )
    assert res_change_model.status_code == 400
    assert "禁止修改 Embedding 模型名称" in res_change_model.json()["detail"]

    res_change_url = client.post(
        "/api/desktop/config/validate",
        json={"new_embedding_base_url": "https://new-endpoint.com/v1"},
        headers=headers,
    )
    assert res_change_url.status_code == 400
    assert "禁止修改 Embedding API 地址" in res_change_url.json()["detail"]


@pytest.mark.asyncio
async def test_baidu_paddleocr_api_success(monkeypatch):
    monkeypatch.setattr(settings, "OCR_MODE", "paddleocr_api")
    monkeypatch.setattr(settings, "BAIDU_OCR_API_URL", "https://aistudio.baidu.com/serving/api/v1/ocr")
    monkeypatch.setattr(settings, "BAIDU_OCR_TOKEN", "mock-token-xyz")

    sample_response = {
        "errorCode": 0,
        "errorMsg": "success",
        "result": {
            "ocrResults": [
                {
                    "prunedResult": {
                        "rec_texts": ["第一章 专业课导论", "1.1 基本概念与考点解析"]
                    }
                }
            ]
        },
    }

    captured_request = {}

    def mock_handler(request: httpx.Request):
        captured_request["url"] = str(request.url)
        captured_request["auth"] = request.headers.get("Authorization")
        captured_request["content_type"] = request.headers.get("Content-Type")
        body = json.loads(request.read())
        captured_request["fileType"] = body.get("fileType")
        captured_request["visualize"] = body.get("visualize")
        return httpx.Response(200, json=sample_response)

    transport = httpx.MockTransport(mock_handler)
    mock_client = httpx.AsyncClient(transport=transport)

    with patch("app.services.http_client.HttpClientProvider.get_client", return_value=mock_client):
        text = await OCRService.extract_text_from_image_bytes(b"dummy_image_data", "png")

    assert text == "第一章 专业课导论\n1.1 基本概念与考点解析"
    assert captured_request["url"] == "https://aistudio.baidu.com/serving/api/v1/ocr"
    assert captured_request["auth"] == "token mock-token-xyz"
    assert captured_request["content_type"] == "application/json"
    assert captured_request["fileType"] == 1
    assert captured_request["visualize"] is False


@pytest.mark.asyncio
async def test_baidu_paddleocr_api_429_quota_exhausted(monkeypatch):
    monkeypatch.setattr(settings, "OCR_MODE", "paddleocr_api")
    monkeypatch.setattr(settings, "BAIDU_OCR_API_URL", "https://aistudio.baidu.com/serving/api/v1/ocr")
    monkeypatch.setattr(settings, "BAIDU_OCR_TOKEN", "mock-token-xyz")

    def mock_handler(request: httpx.Request):
        return httpx.Response(429, json={"errorMsg": "Daily quota exceeded"})

    transport = httpx.MockTransport(mock_handler)
    mock_client = httpx.AsyncClient(transport=transport)

    with patch("app.services.http_client.HttpClientProvider.get_client", return_value=mock_client):
        with pytest.raises(OCRQuotaExhaustedError) as exc_info:
            await OCRService.extract_text_from_image_bytes(b"dummy_image_data", "png")

    assert "额度耗尽" in str(exc_info.value) or "HTTP 429" in str(exc_info.value)


@pytest.mark.asyncio
async def test_baidu_paddleocr_api_403_no_retry(monkeypatch):
    monkeypatch.setattr(settings, "OCR_MODE", "paddleocr_api")
    monkeypatch.setattr(settings, "BAIDU_OCR_API_URL", "https://aistudio.baidu.com/serving/api/v1/ocr")
    monkeypatch.setattr(settings, "BAIDU_OCR_TOKEN", "mock-token-xyz")

    attempts = 0

    def mock_handler(request: httpx.Request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(403, text="Invalid token")

    transport = httpx.MockTransport(mock_handler)
    mock_client = httpx.AsyncClient(transport=transport)

    with patch("app.services.http_client.HttpClientProvider.get_client", return_value=mock_client):
        with pytest.raises(OCRServiceError):
            await OCRService.extract_text_from_image_bytes(b"dummy_image_data", "png")

    # 403 must NOT be retried (exactly 1 attempt)
    assert attempts == 1


def test_backup_and_restore_cycle(isolated_desktop_env, tmp_path):
    from app.backup import create_desktop_backup, restore_desktop_backup

    # Create dummy data
    with db_session() as db:
        db.execute("CREATE TABLE IF NOT EXISTS sample_desktop_item (id TEXT PRIMARY KEY, title TEXT)")
        db.execute("INSERT OR REPLACE INTO sample_desktop_item VALUES ('i1', '备考重点一')")

    backup_dir = tmp_path / "backups_target"
    archive = create_desktop_backup(destination_dir=backup_dir)
    assert archive.is_file()
    assert archive.name.endswith(".tar.gz")

    # Modify the original data
    with db_session() as db:
        db.execute("INSERT OR REPLACE INTO sample_desktop_item VALUES ('i2', '修改前数据')")

    # Restore from the archive
    res = restore_desktop_backup(archive)
    assert res["ok"] is True

    # Verify that data rolled back to exact backup state (i1 exists, i2 does not)
    with db_session() as db:
        rows = db.execute("SELECT id, title FROM sample_desktop_item").fetchall()
        ids = [r[0] for r in rows]
        assert "i1" in ids
        assert "i2" not in ids

    # Verify that pre-restore snapshot directory was preserved
    snapshot_path = Path(res["pre_restore_snapshot"])
    assert snapshot_path.is_dir()
    assert "-pre-restore-" in snapshot_path.name


def test_maintenance_mode_rejects_mutating_user_writes(isolated_desktop_env):
    from app.desktop import MaintenanceManager
    client = TestClient(app)
    headers = {
        "X-RAG-Instance-Token": "test-secret-token-12345",
        "Host": "127.0.0.1:18764",
        "Origin": "http://127.0.0.1:18764",
    }

    # Acquire maintenance lease
    MaintenanceManager.acquire("lease-test-1", "系统维护中")
    try:
        # GET is allowed
        res_get = client.get("/api/materials", headers=headers)
        assert res_get.status_code == 200

        # Desktop maintenance APIs are allowed
        res_desktop = client.get("/api/desktop/status", headers=headers)
        assert res_desktop.status_code == 200
        assert res_desktop.json()["maintenance"] is True

        # Mutating user write to non-desktop route is rejected with 503
        res_post = client.post(
            "/api/materials/upload",
            headers=headers,
        )
        assert res_post.status_code == 503
        assert res_post.json()["detail"]["code"] == "MAINTENANCE_IN_PROGRESS"
    finally:
        MaintenanceManager.release("lease-test-1")


def test_active_operations_block_idle_check(isolated_desktop_env):
    # Ensure request_operations table exists and insert an active processing operation
    with db_session() as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS request_operations (
               id TEXT PRIMARY KEY,
               route TEXT,
               idempotency_key TEXT,
               payload_hash TEXT,
               state TEXT,
               result_json TEXT,
               error_json TEXT,
               updated_at TIMESTAMP
            )"""
        )
        db.execute(
            """INSERT OR REPLACE INTO request_operations(id, route, idempotency_key, payload_hash, state)
               VALUES('op-1', '/api/practice/score', 'idem-1', 'hash1', 'processing')"""
        )

    idle, reason = check_system_idle()
    assert idle is False
    assert "正在进行" in reason

    # After marking completed, system becomes idle
    with db_session() as db:
        db.execute("UPDATE request_operations SET state = 'completed' WHERE id = 'op-1'")

    idle, reason = check_system_idle()
    assert idle is True


def test_backup_target_path_and_preservation(isolated_desktop_env, tmp_path):
    from app.backup import create_desktop_backup

    custom_output = tmp_path / "my_custom_folder" / "specific_name.tar.gz"
    other_file = tmp_path / "my_custom_folder" / "dont_touch_me.tar.gz"
    other_file.parent.mkdir(parents=True)
    other_file.write_text("dummy")

    # Export to exact target_path
    archive = create_desktop_backup(target_path=custom_output)
    assert archive == custom_output.resolve()
    assert archive.is_file()

    # Verify other files in user's folder were NOT pruned
    assert other_file.is_file()


def test_restore_rejects_mismatched_embedding_model(isolated_desktop_env, tmp_path):
    from app.backup import create_desktop_backup, restore_desktop_backup

    # Create dummy data with vectors and embedding_model 'model-A'
    with db_session() as db:
        db.execute("INSERT INTO documents (id, filename, file_hash, file_type, status) VALUES ('d_emb', 't.pdf', 'h1', 'textbook', 'completed')")
        db.execute("INSERT INTO document_chunks (doc_id, chunk_index, content, embedding) VALUES ('d_emb', 0, 'c', X'000102')")

    settings.EMBEDDING_MODEL = "model-A"
    archive = create_desktop_backup(destination_dir=tmp_path / "emb_backup")

    # Attempt restore expecting 'model-B' should be rejected to protect vector space
    with pytest.raises(ValueError) as exc:
        restore_desktop_backup(archive, expected_embedding_model="model-B")
    assert "向量模型 (model-A) 与当前配置 (model-B) 不一致" in str(exc.value)


@pytest.mark.asyncio
async def test_baidu_paddleocr_api_malformed_and_retries(monkeypatch):
    monkeypatch.setattr(settings, "OCR_MODE", "paddleocr_api")
    monkeypatch.setattr(settings, "BAIDU_OCR_API_URL", "https://aistudio.baidu.com/serving/api/v1/ocr")
    monkeypatch.setattr(settings, "BAIDU_OCR_TOKEN", "mock-token")

    # Case 1: missing errorCode
    def handler_missing_err(req):
        return httpx.Response(200, json={"result": {"ocrResults": [{"prunedResult": {"rec_texts": ["hello"]}}]}})

    transport = httpx.MockTransport(handler_missing_err)
    with patch("app.services.http_client.HttpClientProvider.get_client", return_value=httpx.AsyncClient(transport=transport)):
        with pytest.raises(OCRServiceError) as exc:
            await OCRService.extract_text_from_image_bytes(b"img", "png")
        assert "缺少 errorCode 字段" in str(exc.value)

    # Case 2: non-zero errorCode
    def handler_nonzero_err(req):
        return httpx.Response(200, json={"errorCode": 1001, "errorMsg": "参数非法"})

    transport = httpx.MockTransport(handler_nonzero_err)
    with patch("app.services.http_client.HttpClientProvider.get_client", return_value=httpx.AsyncClient(transport=transport)):
        with pytest.raises(OCRServiceError) as exc:
            await OCRService.extract_text_from_image_bytes(b"img", "png")
        assert "参数非法" in str(exc.value)

    # Case 3: ocrResults length is 2 (must be exactly 1 for single image)
    def handler_multipage(req):
        return httpx.Response(200, json={"errorCode": 0, "result": {"ocrResults": [{}, {}]}})

    transport = httpx.MockTransport(handler_multipage)
    with patch("app.services.http_client.HttpClientProvider.get_client", return_value=httpx.AsyncClient(transport=transport)):
        with pytest.raises(OCRServiceError) as exc:
            await OCRService.extract_text_from_image_bytes(b"img", "png")
        assert "单页识别结果数量异常" in str(exc.value)

    # Case 4: rec_texts containing non-string
    def handler_nonstring(req):
        return httpx.Response(200, json={"errorCode": 0, "result": {"ocrResults": [{"prunedResult": {"rec_texts": [123, True]}}]}})

    transport = httpx.MockTransport(handler_nonstring)
    with patch("app.services.http_client.HttpClientProvider.get_client", return_value=httpx.AsyncClient(transport=transport)):
        with pytest.raises(OCRServiceError) as exc:
            await OCRService.extract_text_from_image_bytes(b"img", "png")
        assert "包含非字符串元素" in str(exc.value)

    # Case 5: 500 error verifies exactly 2 retries (total 3 attempts)
    call_count = 0
    def handler_500(req):
        nonlocal call_count
        call_count += 1
        return httpx.Response(500, text="Internal Server Error")

    transport = httpx.MockTransport(handler_500)
    with patch("app.services.http_client.HttpClientProvider.get_client", return_value=httpx.AsyncClient(transport=transport)):
        with pytest.raises(OCRServiceError):
            await OCRService.extract_text_from_image_bytes(b"img", "png")

    assert call_count == 3  # Initial attempt + 2 retries
