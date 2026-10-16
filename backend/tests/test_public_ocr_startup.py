"""Public startup accepts supported remote OCR without invoking providers."""
from __future__ import annotations

import asyncio

import pytest

from app import main
from app.config import settings


@pytest.fixture
def public_startup(monkeypatch):
    for name, value in {
        "PUBLIC_MODE": True,
        "PUBLIC_ORIGIN": "https://study.example.test",
        "AUTH_COOKIE_SECURE": True,
        "DEMO_MODE": False,
        "OCR_MODE": "paddleocr_api",
        "EMBEDDING_MODE": "remote",
    }.items():
        monkeypatch.setattr(settings, name, value)

    calls = []
    events = []

    def record(name):
        def run():
            calls.append(name)
        return run

    def async_record(name):
        async def run():
            calls.append(name)
        return run

    # Only exercise the actual lifespan/configuration boundary. No persistent
    # account databases, HTTP clients, background workers, or providers are used.
    monkeypatch.setattr(main, "setup_diagnostics", lambda: None)
    monkeypatch.setattr(main, "shutdown_diagnostics", lambda: None)
    monkeypatch.setattr(main, "_startup_dump_armed", False)
    monkeypatch.setattr(main, "log_diagnostic", lambda event, **data: events.append({"event": event, **data}))
    monkeypatch.setattr(main, "init_auth_db", record("auth_db"))
    monkeypatch.setattr(main, "enabled_user_ids", lambda: ["00000000-0000-0000-0000-000000000001"])
    monkeypatch.setattr(main, "init_db", record("tenant_db"))
    monkeypatch.setattr(main.HttpClientProvider, "start", async_record("http_start"))
    monkeypatch.setattr(main.HttpClientProvider, "close", async_record("http_close"))
    monkeypatch.setattr(main.TaskWorker, "start_all", record("workers_start"))
    monkeypatch.setattr(main.TaskWorker, "stop_all", async_record("workers_stop"))
    monkeypatch.setattr(main, "stop_operations", async_record("operations_stop"))

    async def run():
        async with main.lifespan(main.app):
            calls.append("serving")

    return lambda: asyncio.run(run()), calls, events


@pytest.mark.parametrize("ocr_mode", ["vision_llm", "paddleocr_api"])
def test_public_lifespan_accepts_supported_remote_ocr(public_startup, monkeypatch, ocr_mode):
    monkeypatch.setattr(settings, "OCR_MODE", ocr_mode)
    run, calls, events = public_startup

    run()

    assert calls == [
        "auth_db", "tenant_db", "http_start", "workers_start", "serving",
        "workers_stop", "operations_stop", "http_close",
    ]
    assert "startup.ready" in [entry["event"] for entry in events]
    assert "shutdown.ready" in [entry["event"] for entry in events]
    assert not any(entry["event"] == "startup.failed" for entry in events)


@pytest.mark.parametrize(
    ("name", "value", "error"),
    [
        ("OCR_MODE", "mock", "OCR_MODE"),
        ("OCR_MODE", "local", "OCR_MODE"),
        ("OCR_MODE", "unknown-remote", "OCR_MODE"),
        ("DEMO_MODE", True, "DEMO_MODE"),
        ("EMBEDDING_MODE", "mock", "EMBEDDING_MODE"),
        ("EMBEDDING_MODE", "unknown-remote", "EMBEDDING_MODE"),
        ("PUBLIC_ORIGIN", "http://study.example.test", "PUBLIC_ORIGIN"),
        ("PUBLIC_ORIGIN", "https://study.example.test/path", "PUBLIC_ORIGIN"),
        ("AUTH_COOKIE_SECURE", False, "secure cookies"),
    ],
)
def test_public_lifespan_rejects_unsafe_configuration_before_starting(
    public_startup, monkeypatch, name, value, error,
):
    monkeypatch.setattr(settings, name, value)
    run, calls, events = public_startup

    with pytest.raises(RuntimeError, match=error):
        run()

    assert calls == []
    assert not any(entry["event"] == "startup.ready" for entry in events)
    failure, = [entry for entry in events if entry["event"] == "startup.failed"]
    assert failure["failed_stage"] == "configuration"
    assert failure["error_type"] == "RuntimeError"
    assert failure["exception_info"]["failed_stage"] == "configuration"
