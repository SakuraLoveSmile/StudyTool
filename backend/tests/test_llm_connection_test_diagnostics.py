"""
Regression tests for the LLM connectivity-test diagnostics (T2, 2026-09-15 round).

Pins the contract introduced by this round:

  * every test run mints a `call_id` that is returned to the caller AND written to the log,
    so a user-visible failure can be found by searching the log page;
  * failures are classified into stable, searchable `error_type` codes and carry model,
    sanitized endpoint, elapsed time, failed stage, HTTP status and upstream request id;
  * transport failures additionally carry a safe exception chain and stack frames;
  * upstream error bodies are reduced to a masked, length-limited code + summary, and a
    non-JSON body is described by its format — never by its content;
  * the API key and the model's answer never reach the log file.

The transport is stubbed at `httpx.AsyncClient.post` so no network is required.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.desktop import (
    _classify_llm_exception,
    _classify_llm_http_status,
    _extract_llm_message_content,
    _extract_upstream_error_summary,
)
from app.diagnostics import setup_diagnostics, shutdown_diagnostics
from app.main import app

# Distinctive canaries: neither may ever appear in a response or a log record.
SECRET_KEY = "sk-live-LEAKCANARY1234567890"
MODEL_ANSWER_MARKER = "MODEL_ANSWER_LEAKCANARY"

DESKTOP_TOKEN = "test-secret-token-12345"
HEADERS = {"X-StudyTool-Instance-Token": DESKTOP_TOKEN, "Host": "127.0.0.1:18764"}
BASE_URL = "https://api.example.com/v1"
MODEL = "gpt-test-model"

Responder = Callable[[str, Dict[str, Any]], Union[httpx.Response, BaseException]]


@pytest.fixture
def desktop_env(monkeypatch, tmp_path):
    """Desktop-mode settings so the instance-token middleware admits the test client."""
    data_dir = tmp_path / "desktop_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(settings, "DESKTOP_MODE", True)
    monkeypatch.setattr(settings, "DESKTOP_INSTANCE_TOKEN", DESKTOP_TOKEN)
    monkeypatch.setattr(settings, "HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "PORT", 18764)
    monkeypatch.setattr(settings, "PUBLIC_MODE", False)
    monkeypatch.setattr(settings, "LLM_API_KEY", "")
    return TestClient(app)


@pytest.fixture
def log_dir(tmp_path):
    """Real JSONL diagnostics output, read back exactly as the desktop log page would."""
    target = tmp_path / "logs"
    shutdown_diagnostics()
    setup_diagnostics(log_level="DEBUG", log_dir=target)
    yield target
    shutdown_diagnostics()


@pytest.fixture
def stub_post(monkeypatch):
    """Patches the throwaway AsyncClient the probe builds, without touching the network."""
    def install(responder: Responder):
        async def fake_post(self, url, **kwargs):
            outcome = responder(url, kwargs)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    return install


def read_entries(log_dir: Path, event: Optional[str] = None) -> List[Dict[str, Any]]:
    target = log_dir / "backend.jsonl"
    entries = [
        json.loads(line)
        for line in target.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if event is None:
        return entries
    return [e for e in entries if e.get("event") == event]


def read_raw(log_dir: Path) -> str:
    return (log_dir / "backend.jsonl").read_text(encoding="utf-8")


def call_probe(client: TestClient, *, api_key: Optional[str] = SECRET_KEY, **overrides):
    body: Dict[str, Any] = {
        "service_type": "llm",
        "base_url": BASE_URL,
        "model": MODEL,
        **overrides,
    }
    if api_key is not None:
        body["api_key"] = api_key
    res = client.post("/api/desktop/test-connection", json=body, headers=HEADERS)
    assert res.status_code == 200, res.text
    return res.json()


def ok_response(content: str, *, request_id: Optional[str] = "upstream-req-1") -> httpx.Response:
    headers = {"content-type": "application/json"}
    if request_id:
        headers["x-request-id"] = request_id
    return httpx.Response(
        200,
        json={"choices": [{"message": {"role": "assistant", "content": content}}]},
        headers=headers,
    )


# --------------------------------------------------------------------------------------
# Helper unit tests
# --------------------------------------------------------------------------------------

def test_extract_upstream_error_summary_keeps_code_and_masks_message():
    res = httpx.Response(
        401,
        json={"error": {"code": "invalid_api_key", "message": f"Incorrect API key: {SECRET_KEY}"}},
        headers={"content-type": "application/json"},
    )
    summary = _extract_upstream_error_summary(res)
    assert summary["reason_code"] == "invalid_api_key"
    assert summary["body_format"] == "application/json"
    assert SECRET_KEY not in summary["detail"]
    assert "sk-live***" in summary["detail"]


def test_extract_upstream_error_summary_truncates_long_message():
    long_message = "x" * 5000
    res = httpx.Response(
        400,
        json={"error": {"code": "bad_request", "message": long_message}},
        headers={"content-type": "application/json"},
    )
    summary = _extract_upstream_error_summary(res)
    assert len(summary["detail"]) <= 300


def test_extract_upstream_error_summary_describes_non_json_body_without_content():
    res = httpx.Response(
        502,
        text="<html><body>SECRET_GATEWAY_PAGE_MARKER</body></html>",
        headers={"content-type": "text/html; charset=utf-8"},
    )
    summary = _extract_upstream_error_summary(res)
    assert summary["reason_code"] == "non_json_error_body"
    assert summary["body_format"] == "text/html"
    assert "detail" not in summary
    assert "SECRET_GATEWAY_PAGE_MARKER" not in json.dumps(summary)


def test_extract_llm_message_content_rejects_unexpected_shapes():
    assert _extract_llm_message_content({"choices": [{"message": {"content": "hi"}}]}) == "hi"
    assert _extract_llm_message_content({"choices": []}) is None
    assert _extract_llm_message_content({"choices": [{}]}) is None
    assert _extract_llm_message_content({"choices": [{"message": {"content": 42}}]}) is None
    assert _extract_llm_message_content([1, 2]) is None


def test_status_and_exception_classification_is_stable():
    assert _classify_llm_http_status(401) == "auth_failed"
    assert _classify_llm_http_status(403) == "permission_denied"
    assert _classify_llm_http_status(429) == "rate_limited"
    assert _classify_llm_http_status(500) == "upstream_error"
    assert _classify_llm_http_status(503) == "upstream_error"
    assert _classify_llm_http_status(418) == "upstream_rejected"

    assert _classify_llm_exception(httpx.ConnectTimeout("t")) == "connect_timeout"
    assert _classify_llm_exception(httpx.ReadTimeout("t")) == "read_timeout"
    assert _classify_llm_exception(httpx.ConnectError("c")) == "connect_failed"
    assert _classify_llm_exception(RuntimeError("boom")) == "unexpected_error"


# --------------------------------------------------------------------------------------
# HTTP status failures
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status,error_type,reason_code",
    [
        (401, "auth_failed", "invalid_api_key"),
        (403, "permission_denied", "permission_denied"),
        (429, "rate_limited", "rate_limit_exceeded"),
        (500, "upstream_error", "server_error"),
        (503, "upstream_error", "overloaded"),
    ],
)
def test_upstream_http_failures_are_classified_and_logged(
    desktop_env, log_dir, stub_post, status, error_type, reason_code
):
    stub_post(
        lambda url, kwargs: httpx.Response(
            status,
            json={"error": {"code": reason_code, "message": "upstream says no"}},
            headers={"content-type": "application/json", "x-request-id": "req-abc-123"},
        )
    )

    payload = call_probe(desktop_env)

    assert payload["ok"] is False
    assert payload["service"] == "llm"
    assert payload["status_code"] == status
    assert payload["error_type"] == error_type
    assert payload["message"]
    assert payload["call_id"].startswith("call-")

    failures = read_entries(log_dir, "llm_test_failed")
    assert len(failures) == 1
    entry = failures[0]
    # The returned diagnostic id must be findable in the log.
    assert entry["call_id"] == payload["call_id"]
    assert entry["service"] == "llm"
    assert entry["model"] == MODEL
    assert entry["status_code"] == status
    assert entry["error_type"] == error_type
    assert entry["failed_stage"] == "http_status"
    assert entry["reason_code"] == reason_code
    assert entry["provider_request_id"] == "req-abc-123"
    assert entry["endpoint"] == "https://api.example.com/v1/chat/completions"
    assert isinstance(entry["duration_ms"], (int, float))
    assert entry["detail"] == "upstream says no"
    assert "stack_frames" not in entry  # HTTP failures need no stack


def test_http_failure_endpoint_is_sanitized_and_query_stripped(desktop_env, log_dir, stub_post):
    stub_post(
        lambda url, kwargs: httpx.Response(
            401, json={"error": {"code": "invalid_api_key"}}, headers={"content-type": "application/json"}
        )
    )
    call_probe(desktop_env, base_url="https://api.example.com/v1?token=SHOULD_NOT_BE_LOGGED")

    entry = read_entries(log_dir, "llm_test_failed")[0]
    assert "?" not in entry["endpoint"]
    assert "SHOULD_NOT_BE_LOGGED" not in read_raw(log_dir)


# --------------------------------------------------------------------------------------
# Transport failures
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "exc,error_type",
    [
        (httpx.ConnectError("connection refused"), "connect_failed"),
        (httpx.ConnectTimeout("connect timed out"), "connect_timeout"),
        (httpx.ReadTimeout("read timed out"), "read_timeout"),
    ],
)
def test_transport_failures_carry_safe_exception_chain_and_stack(
    desktop_env, log_dir, stub_post, exc, error_type
):
    # A cause chain is attached the way httpx wraps lower-level transport errors.
    raised = exc
    try:
        raise OSError(111, "Connection refused")
    except OSError as cause:
        raised = exc
        raised.__cause__ = cause

    stub_post(lambda url, kwargs: raised)

    payload = call_probe(desktop_env)

    assert payload["ok"] is False
    assert payload["error_type"] == error_type
    assert payload["call_id"].startswith("call-")
    assert "status_code" not in payload  # no HTTP response was ever received

    entry = read_entries(log_dir, "llm_test_failed")[0]
    assert entry["error_type"] == error_type
    assert entry["call_id"] == payload["call_id"]
    assert entry["endpoint"] == "https://api.example.com/v1/chat/completions"
    assert entry["model"] == MODEL
    assert isinstance(entry["duration_ms"], (int, float))

    info = entry["exception_info"]
    assert info["exception_type"] == type(exc).__name__
    assert info["failed_stage"] == "request_send"
    assert info["frames"], "network failures must record the stack frames"
    assert any(f["file"] == "desktop.py" for f in info["frames"])
    assert "OSError" in info["cause_chain"]


def test_unexpected_exception_is_logged_without_credentials(desktop_env, log_dir, stub_post):
    stub_post(lambda url, kwargs: RuntimeError(f"boom while using {SECRET_KEY}"))

    payload = call_probe(desktop_env)

    assert payload["ok"] is False
    assert payload["error_type"] == "unexpected_error"
    entry = read_entries(log_dir, "llm_test_failed")[0]
    assert entry["error_type"] == "unexpected_error"
    assert SECRET_KEY not in read_raw(log_dir)
    assert "sk-live***" in entry["exception_info"]["message"]


# --------------------------------------------------------------------------------------
# Malformed 200 responses
# --------------------------------------------------------------------------------------

def test_non_json_200_body_is_reported_as_invalid_json(desktop_env, log_dir, stub_post):
    stub_post(
        lambda url, kwargs: httpx.Response(
            200,
            text="<html>gateway</html>",
            headers={"content-type": "text/html", "x-request-id": "req-html-1"},
        )
    )

    payload = call_probe(desktop_env)

    assert payload["ok"] is False
    assert payload["error_type"] == "invalid_json"
    assert payload["status_code"] == 200
    entry = read_entries(log_dir, "llm_test_failed")[0]
    assert entry["failed_stage"] == "response_parse"
    assert entry["exception_type"] == "JSONDecodeError"
    assert entry["note"] == "text/html"
    assert entry["provider_request_id"] == "req-html-1"


@pytest.mark.parametrize(
    "body",
    [
        {"choices": []},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": None}}]},
        {"unexpected": "shape"},
    ],
)
def test_structural_anomalies_are_reported_as_unexpected_response(
    desktop_env, log_dir, stub_post, body
):
    stub_post(
        lambda url, kwargs: httpx.Response(
            200, json=body, headers={"content-type": "application/json"}
        )
    )

    payload = call_probe(desktop_env)

    assert payload["ok"] is False
    assert payload["error_type"] == "unexpected_response"
    assert payload["status_code"] == 200
    entry = read_entries(log_dir, "llm_test_failed")[0]
    assert entry["failed_stage"] == "response_structure"
    assert entry["status_code"] == 200


# --------------------------------------------------------------------------------------
# Missing credentials and success path
# --------------------------------------------------------------------------------------

def test_missing_api_key_is_reported_and_logged(desktop_env, log_dir, stub_post):
    # No request may be attempted at all.
    stub_post(lambda url, kwargs: pytest.fail("no HTTP request should be made without a key"))

    payload = call_probe(desktop_env, api_key=None)

    assert payload["ok"] is False
    assert payload["error_type"] == "missing_credentials"
    assert payload["call_id"].startswith("call-")
    entry = read_entries(log_dir, "llm_test_failed")[0]
    assert entry["failed_stage"] == "precheck"
    assert entry["call_id"] == payload["call_id"]
    assert entry["duration_ms"] == 0.0


def test_success_path_logs_start_and_success_without_model_answer(desktop_env, log_dir, stub_post):
    stub_post(lambda url, kwargs: ok_response(f'{{"status":"ok","code":200}} {MODEL_ANSWER_MARKER}'))

    payload = call_probe(desktop_env)

    assert payload["ok"] is True
    assert payload["service"] == "llm"
    assert "成功" in payload["message"]
    assert payload["call_id"].startswith("call-")

    started = read_entries(log_dir, "llm_test_started")
    succeeded = read_entries(log_dir, "llm_test_succeeded")
    assert len(started) == 1 and len(succeeded) == 1
    assert started[0]["call_id"] == payload["call_id"]
    assert started[0]["timeout_config"] == 20.0
    assert succeeded[0]["call_id"] == payload["call_id"]
    assert succeeded[0]["status_code"] == 200
    assert succeeded[0]["provider_request_id"] == "upstream-req-1"
    assert isinstance(succeeded[0]["duration_ms"], (int, float))
    assert read_entries(log_dir, "llm_test_failed") == []

    raw = read_raw(log_dir)
    # The probe's own answer and the credential never reach the log.
    assert MODEL_ANSWER_MARKER not in raw
    assert SECRET_KEY not in raw
    assert "Authorization" not in raw


def test_request_uses_bearer_credential_header(desktop_env, stub_post):
    captured: Dict[str, Any] = {}

    def responder(url, kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers")
        return ok_response('{"status":"ok"}')

    stub_post(responder)
    call_probe(desktop_env)

    assert captured["url"] == f"{BASE_URL}/chat/completions"
    assert captured["headers"]["Authorization"] == f"Bearer {SECRET_KEY}"


def test_call_ids_are_unique_per_test(desktop_env, log_dir, stub_post):
    stub_post(lambda url, kwargs: ok_response('{"status":"ok"}'))

    first = call_probe(desktop_env)
    second = call_probe(desktop_env)

    assert first["call_id"] != second["call_id"]
    ids = {e["call_id"] for e in read_entries(log_dir, "llm_test_succeeded")}
    assert ids == {first["call_id"], second["call_id"]}


# --------------------------------------------------------------------------------------
# Leakage canary across every logged field
# --------------------------------------------------------------------------------------

def test_credentials_and_answers_never_reach_the_log_file(desktop_env, log_dir, stub_post):
    """A single run per outcome with a canary in every secret-bearing position."""
    secret_body = {
        "error": {
            "code": "invalid_api_key",
            "message": f"Incorrect API key provided: {SECRET_KEY}",
        }
    }

    stub_post(
        lambda url, kwargs: httpx.Response(
            401, json=secret_body, headers={"content-type": "application/json"}
        )
    )
    call_probe(desktop_env)

    stub_post(lambda url, kwargs: ok_response(f'{{"status":"ok"}} {MODEL_ANSWER_MARKER}'))
    call_probe(desktop_env)

    raw = read_raw(log_dir)
    assert raw  # the log file is actually populated
    assert SECRET_KEY not in raw
    assert MODEL_ANSWER_MARKER not in raw
    # The provider's reason is retained but masked and length-limited: that is what makes
    # the failure actionable, so it must survive scrubbing rather than be dropped wholesale.
    assert "sk-live***" in raw
    assert "invalid_api_key" in raw
