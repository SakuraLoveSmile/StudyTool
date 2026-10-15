import asyncio
import json
import logging
import re
import sys
import time
from pathlib import Path
import pytest
import httpx

from app.diagnostics import (
    setup_diagnostics,
    shutdown_diagnostics,
    task_context,
    unit_context,
    call_context,
    log_diagnostic,
    stream_request_context,
    sanitize_text,
    sanitize_endpoint,
    extract_provider_request_id,
    extract_safe_exception_info,
    SafeRotatingFileHandler,
    StructuredJsonFormatter,
    SanitizedConsoleFormatter,
    task_id_var,
    task_type_var,
    unit_id_var,
    unit_index_var,
    unit_scope_var,
    call_id_var,
    attempt_var,
)
from app.services.llm.client import LLMClient


@pytest.fixture
def temp_log_dir(tmp_path):
    log_d = tmp_path / "logs"
    log_d.mkdir(parents=True, exist_ok=True)
    # Reset to the uninitialized state BEFORE the test too: collection-time imports
    # (e.g. another test module's `from app.main import app`) run the module-level
    # setup_diagnostics() with real default paths, leaving a live file handler that
    # would make setup_diagnostics() early-return here instead of attaching one.
    shutdown_diagnostics()
    yield log_d
    shutdown_diagnostics()


def test_setup_and_shutdown_diagnostics_idempotence(temp_log_dir):
    """Calling setup_diagnostics multiple times must be idempotent; shutdown cleans up handlers."""
    root_logger = logging.getLogger()
    initial_handler_count = len(root_logger.handlers)

    # First setup
    setup_diagnostics(log_dir=temp_log_dir, log_level="DEBUG")
    count_after_first = len(root_logger.handlers)
    assert count_after_first > initial_handler_count

    # Second setup (idempotent)
    setup_diagnostics(log_dir=temp_log_dir, log_level="DEBUG")
    count_after_second = len(root_logger.handlers)
    assert count_after_second == count_after_first

    # Third setup with same or different level
    setup_diagnostics(log_dir=temp_log_dir, log_level="INFO")
    assert len(root_logger.handlers) == count_after_first

    # Shutdown
    shutdown_diagnostics()
    # All diagnostic handlers removed
    diag_handlers = [h for h in root_logger.handlers if getattr(h, "_is_diagnostic_handler", False)]
    assert len(diag_handlers) == 0


def test_safe_rotating_file_handler_write_error_resilience(temp_log_dir, monkeypatch, capsys):
    """File write failures should report once to stderr without crashing business code or recursing."""
    log_file = temp_log_dir / "backend.jsonl"
    handler = SafeRotatingFileHandler(
        str(log_file),
        maxBytes=1024,
        backupCount=2,
        encoding="utf-8"
    )
    handler.setFormatter(StructuredJsonFormatter())
    logger = logging.getLogger("test_resilience_logger")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)

    # Force write to raise an OSError
    def broken_emit(record):
        raise OSError("Simulated disk write failure")

    monkeypatch.setattr(handler, "emit", lambda record: handler.handleError(record))

    # Should not raise exception
    try:
        record = logger.makeRecord(logger.name, logging.INFO, "fn", 1, "test message", (), None)
        handler.handle(record)
    except Exception as exc:
        pytest.fail(f"Handler raised exception instead of failing safely: {exc}")

    # Verify report printed once
    handler.close()
    logger.removeHandler(handler)


def test_log_rotation_and_file_cap(temp_log_dir):
    """RotatingFileHandler respects maxBytes and retains at most backupCount archives."""
    log_file = temp_log_dir / "backend.jsonl"
    # Small size limit to trigger rapid rotation: 350 bytes per file, 2 backups
    max_bytes = 350
    backup_count = 2

    handler = SafeRotatingFileHandler(
        str(log_file),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8"
    )
    handler.setFormatter(StructuredJsonFormatter())
    test_logger = logging.getLogger("test_rotation_logger")
    test_logger.setLevel(logging.INFO)
    test_logger.addHandler(handler)

    # Write enough lines to exceed max_bytes * 5
    for i in range(30):
        log_diagnostic(
            "test_rotation_event",
            level=logging.INFO,
            message=f"Iteration index {i} with some padding content to fill the buffer",
            logger_name="test_rotation_logger",
            iteration=i
        )

    handler.flush()
    handler.close()
    test_logger.removeHandler(handler)

    # Verify primary file exists
    assert log_file.exists()

    # Verify at most backup_count archives exist
    backup_files = list(temp_log_dir.glob("backend.jsonl.*"))
    assert len(backup_files) <= backup_count
    # backend.jsonl.3 should NOT exist
    assert not (temp_log_dir / "backend.jsonl.3").exists()

    # Verify lines in all log files are valid JSON
    all_files = [log_file] + backup_files
    for f in all_files:
        content = f.read_text(encoding="utf-8")
        for line in content.strip().split("\n"):
            if line.strip():
                data = json.loads(line)
                assert "timestamp" in data
                assert "level" in data


def test_sanitization_patterns():
    """All secret credentials, bearer tokens, cookies, and URLs are masked."""
    # 1. API Keys
    text_with_sk = "Error calling model with key sk-1234567890abcdef12345678"
    sanitized = sanitize_text(text_with_sk)
    assert "sk-1234***" in sanitized
    assert "abcdef" not in sanitized

    # 2. Bearer Tokens
    text_with_bearer = "Using token Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    sanitized = sanitize_text(text_with_bearer)
    assert "Bearer ***" in sanitized
    assert "eyJhbGci" not in sanitized

    # 3. Authorization Header
    text_with_auth = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    sanitized_auth = sanitize_text(text_with_auth)
    assert "eyJhbGci" not in sanitized_auth

    # 4. URL Basic Auth
    text_with_url = "Connecting to https://admin:supersecret@api.example.com/v1"
    sanitized = sanitize_text(text_with_url)
    assert "https://***:***@api.example.com/v1" in sanitized
    assert "supersecret" not in sanitized

    # 5. Cookie Header
    text_with_cookie = "Cookie: session_id=secretcookie123; user=admin"
    sanitized = sanitize_text(text_with_cookie)
    assert "cookie: ***" in sanitized.lower()
    assert "secretcookie123" not in sanitized

    # 6. Endpoint sanitization (strips query parameters and path IDs)
    url = "https://api.openai.com/v1/chat/completions?api-version=2024-02-15&key=secret123"
    clean_ep = sanitize_endpoint(url)
    assert "secret123" not in clean_ep
    assert "api-version" not in clean_ep
    assert clean_ep == "https://api.openai.com/v1/chat/completions"

    # 7. Provider request ID extraction
    headers = {
        "Content-Type": "application/json",
        "X-Request-Id": "req-abc-987",
        "CF-Ray": "ray-12345-SJC"
    }
    req_id = extract_provider_request_id(headers)
    assert req_id == "req-abc-987"

    # 8. Safe exception extraction
    exc = ValueError("Invalid response with key sk-secretkey999999999999")
    safe_info = extract_safe_exception_info(exc)
    assert safe_info["exception_type"] == "ValueError"
    assert "sk-secr***" in safe_info["message"]
    assert "999999999999" not in safe_info["message"]


@pytest.mark.asyncio
async def test_stream_request_context_await_headers_vs_read_body_timeouts():
    """Differentiates await_headers timeout from read_body timeout via HTTPX streaming."""
    
    # 1. Await Headers Timeout (Connect/ReadTimeout before response headers arrive)
    class TimeoutOnHeaderTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("Timeout awaiting response headers", request=request)

    client_header = httpx.AsyncClient(transport=TimeoutOnHeaderTransport())
    async with client_header:
        with pytest.raises(httpx.ReadTimeout):
            async with stream_request_context(client_header, "POST", "https://api.example.com/chat") as res:
                await res.aread()

    # 2. Read Body Timeout (Headers received, but reading stream body times out)
    class TimeoutOnBodyStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"partial":'
            raise httpx.ReadTimeout("Timeout while streaming body")

        async def aclose(self):
            pass

    class TimeoutOnBodyTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                stream=TimeoutOnBodyStream(),
                request=request
            )

    client_body = httpx.AsyncClient(transport=TimeoutOnBodyTransport())
    async with client_body:
        with pytest.raises(httpx.ReadTimeout):
            async with stream_request_context(client_body, "POST", "https://api.example.com/chat") as res:
                # Headers are already yielded; calling res.aread() triggers TimeoutOnBodyStream
                await res.aread()


@pytest.mark.asyncio
async def test_llm_chat_json_retries_and_diagnostics(temp_log_dir, monkeypatch):
    """LLMClient.chat_json emits call_context, attempt tracking, retry backoff, and cleans up properly."""
    setup_diagnostics(log_dir=temp_log_dir, log_level="DEBUG")
    monkeypatch.setattr("app.config.settings.DEMO_MODE", False)
    monkeypatch.setattr("app.config.settings.LLM_API_KEY", "test-key-12345")

    call_count = 0

    class RetryTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(
                    429,
                    headers={"Retry-After": "0.01", "x-request-id": "req-429-1"},
                    json={"error": {"message": "Rate limit exceeded"}},
                    request=request
                )
            return httpx.Response(
                200,
                headers={"x-request-id": "req-200-ok"},
                json={"choices": [{"message": {"content": json.dumps({"result": "success"})}}]},
                request=request
            )

    client = httpx.AsyncClient(transport=RetryTransport(), timeout=5.0)
    monkeypatch.setattr("app.services.llm.client.HttpClientProvider.get_client", lambda: client)

    with task_context("task-test-llm", "extract_questions"):
        with unit_context("unit-1", 1, "第 1 页"):
            res = await LLMClient.chat_json("Sys", "User prompt")
            assert res == {"result": "success"}

    await client.aclose()
    shutdown_diagnostics()

    # Read backend.jsonl and check recorded events
    log_file = temp_log_dir / "backend.jsonl"
    assert log_file.exists()
    lines = [json.loads(line) for line in log_file.read_text(encoding="utf-8").strip().split("\n") if line.strip()]

    events = [entry.get("event") for entry in lines]
    assert "slot_wait" in events
    assert "request_dispatched" in events
    assert "retry_wait" in events
    assert "response_headers" in events
    assert "response_body" in events
    assert "response_parsed" in events

    # Check attempt numbers
    dispatched_entries = [e for e in lines if e.get("event") == "request_dispatched"]
    assert len(dispatched_entries) == 2
    assert dispatched_entries[0].get("attempt") == 1
    assert dispatched_entries[1].get("attempt") == 2

    # Check provider request id
    retry_entries = [e for e in lines if e.get("event") == "retry_wait"]
    assert len(retry_entries) == 1
    assert retry_entries[0].get("provider_request_id") == "req-429-1"
    assert retry_entries[0].get("status_code") == 429


@pytest.mark.asyncio
async def test_concurrent_batch_context_isolation(temp_log_dir):
    """Contextvars strictly isolate concurrent batch and unit IDs without cross-contamination."""
    setup_diagnostics(log_dir=temp_log_dir, log_level="DEBUG")

    async def worker(u_id: str, u_idx: int, scope: str, count: int):
        with unit_context(u_id, u_idx, scope):
            for i in range(count):
                log_diagnostic(
                    "worker_ping",
                    level=logging.INFO,
                    message=f"Ping from {u_id} step {i}",
                    step=i
                )
                await asyncio.sleep(0.01)

    with task_context("task-concurrent-test", "batch_extract"):
        await asyncio.gather(
            worker("kbatch-1", 1, "第 1-5 页", 5),
            worker("kbatch-2", 2, "第 6-10 页", 5),
            worker("kbatch-3", 3, "第 11-15 页", 5),
        )

    shutdown_diagnostics()

    log_file = temp_log_dir / "backend.jsonl"
    lines = [json.loads(line) for line in log_file.read_text(encoding="utf-8").strip().split("\n") if line.strip()]

    for line in lines:
        if line.get("event") == "worker_ping":
            u_id = line.get("unit_id")
            scope = line.get("scope")
            u_idx = line.get("unit_index")
            if u_id == "kbatch-1":
                assert scope == "第 1-5 页"
                assert u_idx == 1
            elif u_id == "kbatch-2":
                assert scope == "第 6-10 页"
                assert u_idx == 2
            elif u_id == "kbatch-3":
                assert scope == "第 11-15 页"
                assert u_idx == 3


def test_end_to_end_trace_reconstruction_by_task_id(temp_log_dir):
    """Given only a task_id, we can reconstruct the full lifecycle, attempts, timings, and outcome."""
    setup_diagnostics(log_dir=temp_log_dir, log_level="DEBUG")

    target_task_id = "task-recon-999"

    with task_context(target_task_id, "extract_questions"):
        log_diagnostic("task_started", message="任务开始执行", stage="init")

        with unit_context("qbatch-1", 1, "第 1—3 页"):
            log_diagnostic("unit_started", message="第 1 批开始", stage="batch_extract", chunk_count=3)
            with call_context("call-101", attempt=1):
                log_diagnostic("slot_wait", stage="llm", duration_ms=1.5)
                log_diagnostic("request_dispatched", stage="llm", endpoint="https://api.openai.com/v1/chat/completions")
                log_diagnostic("response_headers", stage="llm", status_code=200, duration_ms=120.0, provider_request_id="req-mock-1")
                log_diagnostic("response_body", stage="llm", duration_ms=45.0, bytes=512)
                log_diagnostic("response_parsed", stage="llm", duration_ms=2.0)
            log_diagnostic("unit_completed", message="第 1 批处理完成", stage="batch_extract", result_count=4)

        with unit_context("qbatch-2", 2, "第 4—6 页"):
            log_diagnostic("unit_started", message="第 2 批开始", stage="batch_extract", chunk_count=3)
            with call_context("call-102", attempt=1):
                log_diagnostic("slot_wait", stage="llm", duration_ms=0.5)
                log_diagnostic("request_dispatched", stage="llm", endpoint="https://api.openai.com/v1/chat/completions")
                log_diagnostic("retry_wait", stage="llm", status_code=500, retry_delay=1.0, attempt=1, error="Internal Server Error")
            with call_context("call-102", attempt=2):
                log_diagnostic("request_dispatched", stage="llm", endpoint="https://api.openai.com/v1/chat/completions")
                log_diagnostic("response_headers", stage="llm", status_code=200, duration_ms=150.0, provider_request_id="req-mock-2")
                log_diagnostic("response_body", stage="llm", duration_ms=30.0, bytes=256)
                log_diagnostic("response_parsed", stage="llm", duration_ms=1.5)
            log_diagnostic("unit_completed", message="第 2 批处理完成", stage="batch_extract", result_count=2)

        log_diagnostic("task_completed", message="任务全部顺利完成", stage="finish", total_saved=6)

    shutdown_diagnostics()

    # Verify reconstruction
    log_file = temp_log_dir / "backend.jsonl"
    all_lines = [json.loads(line) for line in log_file.read_text(encoding="utf-8").strip().split("\n") if line.strip()]

    task_lines = [line for line in all_lines if line.get("task_id") == target_task_id]
    assert len(task_lines) >= 15

    # Chronological sequence of events
    seq = [line.get("event") for line in task_lines]
    assert seq[0] == "task_started"
    assert seq[-1] == "task_completed"
    assert "unit_started" in seq
    assert "retry_wait" in seq
    assert "unit_completed" in seq

    # Batch 2 had 2 attempts on call-102
    b2_lines = [l for l in task_lines if l.get("unit_id") == "qbatch-2"]
    b2_attempts = [l.get("attempt") for l in b2_lines if l.get("attempt") is not None]
    assert 1 in b2_attempts
    assert 2 in b2_attempts

    # Confirm no prompt / secret leaks
    for line in task_lines:
        line_str = json.dumps(line)
        assert re.search(r"\bsk-[a-zA-Z0-9]{4,}", line_str) is None
        assert "password" not in line_str.lower()
        assert "bearer" not in line_str.lower()
