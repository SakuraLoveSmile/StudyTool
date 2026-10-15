"""Integration tests: the desktop Baidu OCR connection test drives the real
PP-OCRv6 asynchronous jobs flow (multipart submit → 5s poll → token-less JSONL
download) end to end.

``/api/desktop/test-connection`` is exercised against a mocked transport that
speaks the jobs wire contract while the OCR service code under test stays real,
so these tests fail if the probe ever stops proving text extraction — a
successful image upload alone must never be reported as a working OCR setup.
"""

import contextlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import init_db
from app.main import app
from app.services.http_client import HttpClientProvider

JOBS_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
JOB_ID = "probe-job-0001"
JSONL_URL = "https://ocr-result.example.com/probe.jsonl?X-Amz-Signature=feedface"
TOKEN = "probe-token-abc"

DESKTOP_HEADERS = {
    "X-StudyTool-Instance-Token": "test-secret-token-12345",
    "Host": "127.0.0.1:18764",
    "Origin": "http://127.0.0.1:18764",
}


@pytest.fixture
def isolated_desktop_env(tmp_path, monkeypatch):
    """Same isolation the other desktop tests use (own data dir + instance token)."""
    data_dir = tmp_path / "desktop_data"
    data_dir.mkdir(parents=True)
    monkeypatch.setenv("STUDYTOOL_DESKTOP_DATA_DIR", str(data_dir))
    monkeypatch.setattr(settings, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(settings, "DESKTOP_MODE", True)
    monkeypatch.setattr(settings, "DESKTOP_INSTANCE_TOKEN", "test-secret-token-12345")
    monkeypatch.setattr(settings, "HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "PORT", 18764)
    monkeypatch.setattr(settings, "PUBLIC_MODE", False)
    # Never let a developer's real .env credentials leak into a probe test.
    monkeypatch.setattr(settings, "BAIDU_OCR_API_URL", "")
    monkeypatch.setattr(settings, "BAIDU_OCR_TOKEN", "")

    init_db()
    yield data_dir


def _jsonl(rec_texts):
    return json.dumps({"result": {"ocrResults": [{"prunedResult": {"rec_texts": rec_texts}}]}})


def _done(json_url=JSONL_URL):
    return httpx.Response(200, json={"code": 0, "data": {"state": "done", "resultUrl": {"jsonUrl": json_url}}})


@contextlib.contextmanager
def _patched_jobs_transport(monkeypatch, *, submit=None, poll=None, download=None):
    """Routes jobs-API calls through a MockTransport and records every request."""
    requests = []
    submit = submit or (lambda r: httpx.Response(200, json={"code": 0, "data": {"jobId": JOB_ID}}))
    poll = poll or (lambda r: _done())
    download = download or (lambda r: httpx.Response(200, text=_jsonl(["TEST_OCR_PROBE_2026"])))

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            return submit(request)
        url = str(request.url)
        if url == JSONL_URL:
            return download(request)
        if url.startswith(f"{JOBS_URL}/"):
            return poll(request)
        raise AssertionError(f"unexpected request: {request.method} {url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(HttpClientProvider, "get_client", lambda: client)
    yield requests


def _probe(service_type="baidu_ocr", url=JOBS_URL, token=TOKEN, **extra):
    payload = {"service_type": service_type, "baidu_ocr_url": url, "baidu_ocr_token": token}
    payload.update(extra)
    return TestClient(app).post("/api/desktop/test-connection", json=payload, headers=DESKTOP_HEADERS)


def test_connection_probe_succeeds_only_with_text_from_new_jobs_flow(isolated_desktop_env, monkeypatch):
    with _patched_jobs_transport(monkeypatch) as requests:
        res = _probe()

    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert "有效文字" in body["message"]

    # 1. Multipart submit to the complete jobs address with the entered credentials.
    submit = requests[0]
    assert submit.method == "POST"
    assert str(submit.url) == JOBS_URL
    assert submit.headers["Authorization"] == f"Bearer {TOKEN}"
    assert submit.headers["Content-Type"].startswith("multipart/form-data")
    raw = submit.read()
    assert b"PP-OCRv6" in raw
    assert b"\x89PNG" in raw  # the fitz-rendered synthetic probe image, raw bytes
    assert b"base64" not in raw

    # 2. Poll the created job.
    poll = requests[1]
    assert poll.method == "GET"
    assert str(poll.url) == f"{JOBS_URL}/{JOB_ID}"
    assert poll.headers["Authorization"] == f"Bearer {TOKEN}"

    # 3. The pre-signed result download must not carry the user's token.
    download = requests[2]
    assert str(download.url) == JSONL_URL
    assert "Authorization" not in download.headers


def test_connection_probe_fails_when_recognition_is_empty(isolated_desktop_env, monkeypatch):
    with _patched_jobs_transport(monkeypatch, download=lambda r: httpx.Response(200, text=_jsonl([]))):
        res = _probe()

    body = res.json()
    assert body["ok"] is False
    assert "为空" in body["message"]


def test_connection_probe_fails_when_job_reports_failed(isolated_desktop_env, monkeypatch):
    failed = lambda r: httpx.Response(  # noqa: E731
        200, json={"code": 0, "data": {"state": "failed", "errorMsg": "模型执行失败"}}
    )
    with _patched_jobs_transport(monkeypatch, poll=failed):
        res = _probe()

    body = res.json()
    assert body["ok"] is False
    assert "任务执行失败" in body["message"]


def test_connection_probe_maps_quota_exhaustion(isolated_desktop_env, monkeypatch):
    quota = lambda r: httpx.Response(429, json={"code": 429, "msg": "quota exceeded"})  # noqa: E731
    with _patched_jobs_transport(monkeypatch, submit=quota):
        res = _probe()

    body = res.json()
    assert body["ok"] is False
    assert "额度" in body["message"]


def test_connection_probe_exposes_safe_submit_reason(isolated_desktop_env, monkeypatch):
    submit = lambda r: httpx.Response(400, json={
        "errorCode": "INVALID_ARGUMENT",
        "errorMsg": f"当前凭证 {TOKEN} 不支持所请求的模型",
    })
    with _patched_jobs_transport(monkeypatch, submit=submit) as requests:
        res = _probe()

    body = res.json()
    assert body["ok"] is False
    assert body["message"].startswith("百度 OCR 任务提交失败（HTTP 400）")
    assert "不支持所请求的模型" in body["message"]
    assert "INVALID_ARGUMENT" in body["message"]
    assert TOKEN not in body["message"]
    assert len(requests) == 1


def test_connection_probe_rejects_unknown_job_state(isolated_desktop_env, monkeypatch):
    weird = lambda r: httpx.Response(200, json={"code": 0, "data": {"state": "paused"}})  # noqa: E731
    with _patched_jobs_transport(monkeypatch, poll=weird):
        res = _probe()

    body = res.json()
    assert body["ok"] is False
    assert "任务状态异常" in body["message"]


def test_connection_probe_requires_url_and_token(isolated_desktop_env):
    res = TestClient(app).post(
        "/api/desktop/test-connection",
        json={"service_type": "baidu_ocr", "baidu_ocr_url": "", "baidu_ocr_token": ""},
        headers=DESKTOP_HEADERS,
    )
    assert res.status_code == 400
    assert "未配置" in res.json()["detail"]
