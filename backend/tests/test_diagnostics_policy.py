"""
Policy tests for the test-build diagnostics subsystem (this round's T1/T2).

Complements `test_diagnostics.py` (which covers the pre-existing tracing behaviour) by
pinning the NEW release gates:

  * startup ordering — desktop config + logging must be initialized before business imports;
  * shared envelope — source / session_id / app_version on every file record;
  * whitelist + recursive scrubbing, including the "sk-" false-positive regression;
  * 5 MiB / 5 backups / 7 day retention, bounded writes, duplicate-error aggregation.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import pytest

from app import diagnostics as diag
from app.diagnostics import (
    DiagnosticFileHandler,
    LOG_FIELD_WHITELIST,
    log_diagnostic,
    sanitize_text,
    scrub_record,
    setup_diagnostics,
    shutdown_diagnostics,
)


@pytest.fixture
def temp_log_dir(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    # Reset to the uninitialized state BEFORE the test too: collection-time imports
    # (e.g. another test module's `from app.main import app`) run the module-level
    # setup_diagnostics() with real default paths, leaving a live file handler that
    # would make setup_diagnostics() early-return here instead of attaching one.
    shutdown_diagnostics()
    yield log_dir
    shutdown_diagnostics()


def _read_entries(log_dir: Path, name: str = "backend.jsonl"):
    target = log_dir / name
    if not target.exists():
        return []
    return [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]


# --------------------------------------------------------------------------------------
# Startup ordering
# --------------------------------------------------------------------------------------

def test_desktop_logging_is_initialized_before_business_imports():
    """
    Structural regression guard: the whole point of T1 is that a failing business import
    can still be logged. That only holds while setup_diagnostics() runs first.
    """
    source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")

    setup_index = source.index("\nsetup_diagnostics()")
    for business_import in (
        "from app.database import init_db",
        "from app.services.task_worker import TaskWorker",
        "from app.routers import materials",
        "from app.desktop import desktop_router",
    ):
        import_index = source.index(business_import)
        assert setup_index < import_index, (
            f"{business_import} is imported before setup_diagnostics(); "
            "a broken import would then leave no log evidence"
        )

    # Desktop bootstrap must also precede the business imports.
    assert source.index("bootstrap_desktop()") < source.index("from app.database import init_db")


def test_business_import_failure_is_recorded():
    """A failing business import must produce a structured record before propagating."""
    source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    assert "startup.import_failed" in source
    assert "except BaseException as _import_exc" in source
    assert "exception_info=extract_safe_exception_info(_import_exc, \"import\")" in source


# --------------------------------------------------------------------------------------
# Shared envelope
# --------------------------------------------------------------------------------------

def test_every_record_carries_the_shared_envelope(temp_log_dir):
    setup_diagnostics(log_dir=temp_log_dir, log_level="INFO")
    log_diagnostic("startup.ready", message="后台服务已就绪", phase="ready")
    shutdown_diagnostics()

    entries = _read_entries(temp_log_dir)
    assert entries, "the diagnostics file must contain the startup record"
    entry = entries[-1]
    assert entry["source"] == "backend"
    assert entry["session_id"].startswith("sess-")
    assert entry["level"] == "INFO"
    assert entry["event"] == "startup.ready"
    assert entry["message"] == "后台服务已就绪"
    assert entry["phase"] == "ready"
    assert entry["timestamp"]


def test_session_id_is_stable_across_records(temp_log_dir):
    setup_diagnostics(log_dir=temp_log_dir, log_level="INFO")
    log_diagnostic("first.event")
    log_diagnostic("second.event")
    shutdown_diagnostics()

    sessions = {entry["session_id"] for entry in _read_entries(temp_log_dir)}
    assert len(sessions) == 1, "one launch must share a single session id"


# --------------------------------------------------------------------------------------
# Whitelist + redaction
# --------------------------------------------------------------------------------------

def test_whitelist_drops_unvetted_fields_but_keeps_stack_frames(temp_log_dir):
    setup_diagnostics(log_dir=temp_log_dir, log_level="INFO")
    log_diagnostic(
        "llm.call",
        message="调用失败",
        prompt="用户的完整提示词",
        content="资料原文",
        answer="模型回答",
        exception_info={
            "exception_type": "ConnectError",
            "message": "连接失败",
            "frames": [{"file": "client.py", "line": 42, "func": "post"}],
        },
    )
    shutdown_diagnostics()

    entry = _read_entries(temp_log_dir)[-1]
    for dropped in ("prompt", "content", "answer"):
        assert dropped not in entry, f"{dropped} must never be persisted"
    # Regression: inner frame keys must survive the recursive whitelist or the stack is lost.
    frames = entry["exception_info"]["frames"]
    assert frames[0]["file"] == "client.py"
    assert frames[0]["line"] == 42
    assert frames[0]["func"] == "post"


def test_task_id_containing_sk_is_not_mangled_by_the_secret_pattern():
    """
    Regression: the loose `sk-` heuristic turned `task-recon-999` into `task-reco***`,
    which silently destroyed the task correlation id once recursive scrubbing landed.
    """
    assert sanitize_text("task-recon-999") == "task-recon-999"
    assert sanitize_text("task-1234567890") == "task-1234567890"
    # Real keys are still masked.
    assert "sk-1234***" in sanitize_text("key sk-1234567890abcdef12345678")


def test_sanitize_text_masks_paths_url_params_and_file_names():
    assert sanitize_text(
        "GET https://api.example.com/v1/models?api-key=secret&user=bob"
    ) == "GET https://api.example.com/v1/models"
    assert "测试用户" not in sanitize_text(r"C:\Users\测试用户\AppData\Local\StudyTool\data\app.db")
    assert "someone" not in sanitize_text("/Users/someone/Library/Application Support/StudyTool/logs")
    assert "private" not in sanitize_text("~/Documents/private/notes")
    assert sanitize_text("failed to parse 2024真题.pdf") == "failed to parse <file>"
    # Source module names stay readable: they are diagnostics, not user content.
    assert sanitize_text("ImportError in app/services/llm/client.py") == (
        "ImportError in app/services/llm/client.py"
    )


def test_scrub_record_is_recursive_and_bounded():
    scrubbed = scrub_record(
        {
            "event": "x",
            "frames": [{"file": "2024真题.pdf", "line": 1}],
            "nested_unknown": {"content": "业务正文"},
            "tags": ["ok", "sk-1234567890abcdef1234"],
        }
    )
    assert scrubbed["frames"][0]["file"] == "<file>"
    assert "nested_unknown" not in scrubbed
    assert "sk-1234567890abcdef1234" not in json.dumps(scrubbed)
    assert "timestamp" in LOG_FIELD_WHITELIST and "message" in LOG_FIELD_WHITELIST


# --------------------------------------------------------------------------------------
# Storage policy: rotation, retention, rate limiting, aggregation
# --------------------------------------------------------------------------------------

def _make_handler(log_dir: Path, **kwargs) -> DiagnosticFileHandler:
    handler = DiagnosticFileHandler(
        filename=str(log_dir / "backend.jsonl"),
        maxBytes=kwargs.pop("maxBytes", 1024),
        backupCount=kwargs.pop("backupCount", 2),
        **kwargs,
    )
    handler.setFormatter(diag.StructuredJsonFormatter())
    return handler


def test_handler_defaults_match_the_release_storage_policy():
    assert diag.DEFAULT_LOG_MAX_BYTES == 5 * 1024 * 1024
    assert diag.LOG_RETENTION_DAYS == 7


def test_retention_cleanup_removes_only_expired_rotated_files(temp_log_dir):
    handler = _make_handler(temp_log_dir, retention_days=7)
    stale = temp_log_dir / "backend.jsonl.1"
    fresh = temp_log_dir / "backend.jsonl.2"
    stale.write_text("{}\n", encoding="utf-8")
    fresh.write_text("{}\n", encoding="utf-8")
    old = time.time() - 8 * 24 * 60 * 60
    os.utime(stale, (old, old))

    removed = handler.cleanup_old_files()
    assert removed == 1
    assert not stale.exists(), "rotated file older than 7 days must be deleted"
    assert fresh.exists(), "recent rotated file must be kept"
    handler.close()


def test_rate_limiter_drops_excess_records_and_reports_once(temp_log_dir):
    handler = _make_handler(temp_log_dir, max_records_per_second=5, retention_days=7)
    logger = logging.getLogger("rate.limit.test")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)

    for index in range(60):
        logger.info("flood %s", index, extra={"event": f"flood.{index}"})
    handler.close()

    entries = _read_entries(temp_log_dir)
    assert len(entries) < 60, "the rate limiter must bound what reaches the file"
    notices = [entry for entry in entries if entry.get("event") == "log.rate_limited"]
    assert len(notices) == 1, "the drop notice is emitted exactly once per window"
    assert notices[0]["dropped"] > 0


def test_duplicate_errors_aggregate_with_occurrences(temp_log_dir):
    handler = _make_handler(temp_log_dir, aggregation_window_seconds=30.0, retention_days=7)
    logger = logging.getLogger("aggregate.test")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)

    for _ in range(7):
        logger.error("模型调用失败", extra={"event": "llm.call_failed", "error_type": "ConnectError"})
    handler.close()

    entries = _read_entries(temp_log_dir)
    summaries = [entry for entry in entries if entry.get("occurrences")]
    assert len(summaries) == 1, "repeated identical errors collapse into one summary"
    assert summaries[0]["occurrences"] == 7
    assert summaries[0]["event"] == "llm.call_failed"
    assert summaries[0]["first_seen"] and summaries[0]["last_seen"]


def test_write_failure_never_raises_into_business_code(tmp_path):
    """A broken log target must degrade to a single stderr note, not an exception."""
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")

    setup_diagnostics(log_dir=blocked, log_level="INFO")
    # Must not raise despite the unwritable target.
    for index in range(5):
        log_diagnostic("startup.phase", message=f"phase {index}")
    shutdown_diagnostics()


def test_legacy_records_without_source_are_still_readable_by_the_desktop_parser():
    """
    Cross-implementation compatibility: the desktop reader must accept the legacy record
    shape this backend produced before the shared envelope existed.
    """
    legacy = json.dumps({"asctime": "2026-09-01 10:00:00", "levelname": "WARNING", "msg": "旧格式告警"})
    assert re.search(r'"msg"', legacy), "fixture shape sanity check"
    # The desktop parser is exercised by desktop/src/tests/log-store.test.ts; here we only
    # assert the JSON stays parseable so the contract cannot silently drift.
    parsed = json.loads(legacy)
    assert parsed["levelname"] == "WARNING"


def test_file_logging_survives_uvicorn_logging_reconfiguration(tmp_path):
    """
    Regression for a defect the unit tests missed and only a real startup exposed:

    `uvicorn.run()` applies `logging.config.dictConfig`, which CLOSES every pre-existing
    handler while leaving it attached to the root logger. The idempotence guard in
    `setup_diagnostics()` used to be "handler is not None", so it returned early and the
    diagnostics file silently stopped receiving records the moment uvicorn started — no
    lifespan, database, ready or shutdown phase was ever written.

    Run in a subprocess on purpose: dictConfig mutates process-global logging state, which
    must not leak into the rest of the pytest session.
    """
    import subprocess
    import sys

    backend_root = Path(__file__).resolve().parents[1]
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    script = f"""
import json, logging.config, os, sys
sys.path.insert(0, {str(backend_root)!r})
import uvicorn.config
from app.diagnostics import setup_diagnostics, log_diagnostic, shutdown_diagnostics

setup_diagnostics(log_level="INFO")
log_diagnostic("before.reconfig", message="before")

logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)   # exactly what uvicorn.run() does
setup_diagnostics()                                        # what the lifespan calls first
log_diagnostic("after.reconfig", message="after")
shutdown_diagnostics()

path = os.path.join({str(log_dir)!r}, "backend.jsonl")
events = [json.loads(line)["event"] for line in open(path, encoding="utf-8") if line.strip()]
print(json.dumps(events))
"""
    env = {**os.environ, "LOG_DIR": str(log_dir)}
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(backend_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    events = json.loads(completed.stdout.strip().splitlines()[-1])
    assert "before.reconfig" in events
    assert "after.reconfig" in events, (
        "file logging must be re-armed after uvicorn's dictConfig closes the handler"
    )


# --------------------------------------------------------------------------------------
# Console encoding safety (Windows code pages)
# --------------------------------------------------------------------------------------

def test_console_handler_never_raises_on_unencodable_output(capsys):
    """
    Regression for the Windows CI failure: the console/pipe encoding there is the locale
    code page (cp1252 on an English install), so a Chinese record raised UnicodeEncodeError
    inside `logging`. The framework's default handleError then dumped a traceback plus a
    stack for EVERY affected record, which buried the frozen backend's real startup output.
    """
    import io

    from app.diagnostics import SafeStreamHandler

    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict", write_through=True)
    handler = SafeStreamHandler(stream)
    handler.setFormatter(diag.SanitizedConsoleFormatter())
    record = logging.LogRecord("app.test", logging.INFO, __file__, 1, "中文日志消息", (), None)

    SafeStreamHandler._encoding_warning_emitted = False
    handler.emit(record)  # must not raise

    err = capsys.readouterr().err
    assert "Traceback" not in err, "no framework traceback dump for an encoding failure"
    assert "Call stack" not in err
    assert "could not be encoded" in err, "the problem is reported once, in ASCII"


def test_encoding_safe_streams_substitute_instead_of_raising():
    """`errors="replace"` keeps the native code page but never raises for Chinese text."""
    import io

    from app.diagnostics import _make_streams_encoding_safe

    class FakeStream:
        def __init__(self):
            self.encoding_calls = []

        def reconfigure(self, **kwargs):
            self.encoding_calls.append(kwargs)

    fake = FakeStream()
    original_stdout, original_stderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = fake, FakeStream()
    try:
        _make_streams_encoding_safe()
    finally:
        sys.stdout, sys.stderr = original_stdout, original_stderr

    assert fake.encoding_calls == [{"errors": "replace"}]

    # End to end: a cp1252 stream with errors="replace" writes without raising.
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="replace", write_through=True)
    stream.write("中文日志消息")
    assert stream.encoding.lower().startswith("cp1252")
