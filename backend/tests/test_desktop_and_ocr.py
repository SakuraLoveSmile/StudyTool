import asyncio
import base64
import contextlib
import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

import app.services.parser.ocr_service as ocr_module
from app.config import settings
from app.database import db_session, init_db
from app.desktop import (
    DesktopAccessMiddleware,
    check_system_idle,
    has_existing_embeddings,
)
from app.main import app
from app.services.http_client import GlobalRateLimiter, HttpClientProvider
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


# ---------------------------------------------------------------------------
# Baidu PP-OCRv6 asynchronous jobs API (/api/v2/ocr/jobs).
# These tests replace the removed v1 synchronous protocol coverage
# (test_baidu_paddleocr_api_success / _429_quota_exhausted / _403_no_retry /
#  _malformed_and_retries).
# ---------------------------------------------------------------------------

OCR_JOBS_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
OCR_JOB_ID = "5a1b2c3d4e5f6071"
OCR_JSONL_URL = "https://ocr-result.example.com/out/result.jsonl?X-Amz-Signature=abc123"
OCR_RESULT_TOKEN = "mock-token-xyz"


def _jobs_config(monkeypatch, url=OCR_JOBS_URL, token=OCR_RESULT_TOKEN):
    monkeypatch.setattr(settings, "OCR_MODE", "paddleocr_api")
    monkeypatch.setattr(settings, "BAIDU_OCR_API_URL", url)
    monkeypatch.setattr(settings, "BAIDU_OCR_TOKEN", token)


def _jsonl_lines(*lines) -> str:
    """Each line is a list of per-page recognized-text lists."""
    return "\n".join(
        json.dumps({"result": {"ocrResults": [
            {"prunedResult": {"rec_texts": list(page)}} for page in pages
        ]}}, ensure_ascii=False)
        for pages in lines
    )


@contextlib.contextmanager
def _jobs_client(monkeypatch, handler):
    """Installs a MockTransport-backed shared client and yields observed requests."""
    requests = []

    def recording(request):
        requests.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(recording))
    monkeypatch.setattr(HttpClientProvider, "get_client", lambda: client)
    yield requests


@contextlib.contextmanager
def _no_wait(monkeypatch):
    """Removes poll/retry wall-clock waits while recording the requested delays."""
    delays = []

    async def _sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(ocr_module.asyncio, "sleep", _sleep)
    yield delays


def _jobs_handler(*, submit=None, poll=None, download=None):
    submit = submit or (lambda r: httpx.Response(200, json={"code": 0, "data": {"jobId": OCR_JOB_ID}}))
    poll = poll or (lambda r: httpx.Response(200, json={"code": 0, "data": {
        "state": "done", "resultUrl": {"jsonUrl": OCR_JSONL_URL}}}))
    download = download or (lambda r: httpx.Response(200, text=_jsonl_lines([["默认文字"]])))

    def handler(request):
        if request.method == "POST":
            return submit(request)
        url = str(request.url)
        if url == OCR_JSONL_URL:
            return download(request)
        if url == f"{OCR_JOBS_URL}/{OCR_JOB_ID}":
            return poll(request)
        raise AssertionError(f"unexpected request: {request.method} {url}")

    return handler


@pytest.mark.asyncio
async def test_baidu_paddleocr_jobs_success_flow(monkeypatch):
    """Normal jobs flow: multipart submit → poll → token-less JSONL download."""
    _jobs_config(monkeypatch)

    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"code": 0, "data": {"jobId": OCR_JOB_ID}})
        url = str(request.url)
        if url == f"{OCR_JOBS_URL}/{OCR_JOB_ID}":
            return httpx.Response(200, json={"code": 0, "data": {
                "state": "done", "resultUrl": {"jsonUrl": OCR_JSONL_URL}}})
        if url == OCR_JSONL_URL:
            return httpx.Response(200, text=_jsonl_lines(
                [["第一章 专业课导论", "1.1 基本概念与考点解析"]]
            ))
        raise AssertionError(f"unexpected request: {url}")

    with _no_wait(monkeypatch) as delays:
        with _jobs_client(monkeypatch, handler) as requests:
            text = await OCRService.extract_text_from_image_bytes(b"dummy_image_data", "png")

    assert text == "第一章 专业课导论\n1.1 基本概念与考点解析"
    submit, poll, download = requests[0], requests[1], requests[2]

    assert submit.method == "POST"
    assert str(submit.url) == OCR_JOBS_URL
    assert submit.headers["Authorization"] == f"Bearer {OCR_RESULT_TOKEN}"
    assert submit.headers["Content-Type"].startswith("multipart/form-data")
    body = submit.read()
    assert b"PP-OCRv6" in body
    assert b'name="optionalPayload"' in body
    assert b'"useDocOrientationClassify": false' in body
    assert b'"useDocUnwarping": false' in body
    assert b'"useTextlineOrientation": false' in body
    assert b'"visualize": false' in body
    assert b"dummy_image_data" in body

    assert poll.method == "GET"
    assert str(poll.url) == f"{OCR_JOBS_URL}/{OCR_JOB_ID}"
    assert poll.headers["Authorization"] == f"Bearer {OCR_RESULT_TOKEN}"

    assert str(download.url) == OCR_JSONL_URL
    assert "Authorization" not in download.headers
    assert delays == []


@pytest.mark.asyncio
async def test_baidu_paddleocr_jobs_429_quota_exhausted(monkeypatch):
    _jobs_config(monkeypatch)
    attempts = 0

    def mock_handler(request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, json={"msg": "Daily quota exceeded"})

    with _no_wait(monkeypatch) as delays:
        with _jobs_client(monkeypatch, mock_handler):
            with pytest.raises(OCRQuotaExhaustedError) as exc_info:
                await OCRService.extract_text_from_image_bytes(b"dummy_image_data", "png")

    assert "429" in str(exc_info.value)
    assert attempts == 1  # quota exhaustion is never retried
    assert delays == []


@pytest.mark.asyncio
async def test_baidu_paddleocr_jobs_submit_failure_is_not_retried(monkeypatch):
    """A retried submit would create duplicate jobs: the POST is sent exactly once."""
    _jobs_config(monkeypatch)
    attempts = 0

    def mock_handler(request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(403, text="Invalid token")

    with _no_wait(monkeypatch) as delays:
        with _jobs_client(monkeypatch, mock_handler):
            with pytest.raises(OCRServiceError):
                await OCRService.extract_text_from_image_bytes(b"dummy_image_data", "png")

    assert attempts == 1
    assert delays == []


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
async def test_baidu_paddleocr_jobs_failed_and_unknown_states(monkeypatch):
    _jobs_config(monkeypatch)

    failed_poll = lambda r: httpx.Response(200, json={"code": 0, "data": {
        "state": "failed", "errorMsg": "模型执行失败"}})
    with _no_wait(monkeypatch):
        with _jobs_client(monkeypatch, _jobs_handler(poll=failed_poll)) as requests:
            with pytest.raises(OCRServiceError) as exc:
                await OCRService.extract_text_from_image_bytes(b"img", "png")
    assert "任务执行失败" in str(exc.value)
    assert not any(str(r.url) == OCR_JSONL_URL for r in requests)  # nothing downloaded

    unknown_poll = lambda r: httpx.Response(200, json={"code": 0, "data": {"state": "queued"}})
    with _no_wait(monkeypatch):
        with _jobs_client(monkeypatch, _jobs_handler(poll=unknown_poll)):
            with pytest.raises(OCRServiceError) as exc:
                await OCRService.extract_text_from_image_bytes(b"img", "png")
    assert "状态异常" in str(exc.value)


@pytest.mark.asyncio
async def test_baidu_paddleocr_jobs_missing_contract_fields(monkeypatch):
    _jobs_config(monkeypatch)

    # Missing jobId in the submit response
    no_job_id = lambda r: httpx.Response(200, json={"code": 0, "data": {"state": "pending"}})
    with _no_wait(monkeypatch):
        with _jobs_client(monkeypatch, _jobs_handler(submit=no_job_id)) as requests:
            with pytest.raises(OCRServiceError) as exc:
                await OCRService.extract_text_from_image_bytes(b"img", "png")
    assert "缺少 jobId" in str(exc.value)
    assert len(requests) == 1

    # state=done without resultUrl.jsonUrl
    no_json_url = lambda r: httpx.Response(200, json={"code": 0, "data": {"state": "done"}})
    with _no_wait(monkeypatch):
        with _jobs_client(monkeypatch, _jobs_handler(poll=no_json_url)):
            with pytest.raises(OCRServiceError) as exc:
                await OCRService.extract_text_from_image_bytes(b"img", "png")
    assert "缺少 resultUrl.jsonUrl" in str(exc.value)


@pytest.mark.asyncio
async def test_baidu_paddleocr_jobs_malformed_jsonl_and_empty_text(monkeypatch):
    _jobs_config(monkeypatch)

    malformed = lambda r: httpx.Response(200, text='{"result": {"ocrResults": [')
    with _no_wait(monkeypatch):
        with _jobs_client(monkeypatch, _jobs_handler(download=malformed)):
            with pytest.raises(OCRServiceError) as exc:
                await OCRService.extract_text_from_image_bytes(b"img", "png")
    assert "JSONL" in str(exc.value)

    empty = lambda r: httpx.Response(200, text=_jsonl_lines([["   ", ""]]))
    with _no_wait(monkeypatch):
        with _jobs_client(monkeypatch, _jobs_handler(download=empty)):
            with pytest.raises(OCRServiceError) as exc:
                await OCRService.extract_text_from_image_bytes(b"img", "png")
    assert "识别文本为空" in str(exc.value)


@pytest.mark.asyncio
async def test_baidu_paddleocr_jobs_poll_and_download_retry_limits(monkeypatch):
    _jobs_config(monkeypatch)

    poll_attempts = 0

    def failing_poll(request):
        nonlocal poll_attempts
        poll_attempts += 1
        return httpx.Response(500, text="Internal Server Error")

    with _no_wait(monkeypatch) as delays:
        with _jobs_client(monkeypatch, _jobs_handler(poll=failing_poll)):
            with pytest.raises(OCRServiceError) as exc:
                await OCRService.extract_text_from_image_bytes(b"img", "png")

    assert poll_attempts == 3  # initial + 2 retries
    assert delays == [1.0, 2.0]
    assert "重试耗尽" in str(exc.value)

    download_attempts = 0

    def failing_download(request):
        nonlocal download_attempts
        download_attempts += 1
        return httpx.Response(503, text="Service Unavailable")

    with _no_wait(monkeypatch) as delays:
        with _jobs_client(monkeypatch, _jobs_handler(download=failing_download)):
            with pytest.raises(OCRServiceError):
                await OCRService.extract_text_from_image_bytes(b"img", "png")

    assert download_attempts == 3
    assert delays == [1.0, 2.0]


@pytest.mark.asyncio
async def test_baidu_paddleocr_jobs_hard_timeout(monkeypatch):
    """A job that never completes is abandoned at the 300s page budget."""
    _jobs_config(monkeypatch)
    monkeypatch.setattr(OCRService, "JOB_TOTAL_TIMEOUT", 10.0)

    clock = {"now": 0.0}

    class _Clock:
        def monotonic(self):
            return clock["now"]

    monkeypatch.setattr(ocr_module, "time", _Clock())

    delays = []

    async def _sleep(delay):
        delays.append(delay)
        clock["now"] += delay

    monkeypatch.setattr(ocr_module.asyncio, "sleep", _sleep)

    running = lambda r: httpx.Response(200, json={"code": 0, "data": {"state": "running"}})
    with _jobs_client(monkeypatch, _jobs_handler(poll=running)) as requests:
        with pytest.raises(OCRServiceError) as exc:
            await OCRService.extract_text_from_image_bytes(b"img", "png")

    assert "超时" in str(exc.value)
    assert delays == [5.0, 5.0]  # two 5s polls, then the budget is gone
    assert len(requests) == 3  # submit + 2 polls


@pytest.mark.asyncio
async def test_baidu_paddleocr_jobs_cancellation_releases_resources(monkeypatch):
    """Cancellation closes the open response, frees the limiter slot and propagates."""
    _jobs_config(monkeypatch)
    GlobalRateLimiter._init()
    slot_before = GlobalRateLimiter._semaphore._value

    class _Response:
        status_code = 200
        headers = {}

        def __init__(self):
            self.closed = False

        async def aread(self):
            raise asyncio.CancelledError()

        async def aclose(self):
            self.closed = True

    class _Client:
        def __init__(self, response):
            self.response = response

        async def post(self, url, **kwargs):
            return self.response

    response = _Response()
    monkeypatch.setattr(HttpClientProvider, "get_client", lambda: _Client(response))

    with pytest.raises(asyncio.CancelledError):
        await OCRService.extract_text_from_image_bytes(b"img", "png")

    assert response.closed is True
    assert GlobalRateLimiter._semaphore._value == slot_before
