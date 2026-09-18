"""
D1 (beta.3): a failing log path must never abort backend startup.

`app/main.py` calls `setup_diagnostics()` at module level before any business import.
`settings.log_path` creates the directory on attribute access, so a `logs` path occupied
by a regular file — or refused by the ACL — used to raise out of `setup_diagnostics()`
and kill the process before it could serve. The whole log-target chain (resolution ->
mkdir -> handler create/attach) now shares one protected scope that degrades to a single
`[DIAGNOSTICS INIT ERROR]` stderr note while the console handler still installs and the
function returns normally.

Every test here drives the REAL default path: `setup_diagnostics()` is called with no
`log_dir` argument and `settings.LOG_DIR` is monkeypatched — `settings` itself is never
mocked, so `settings.log_path` (including its mkdir-on-access) executes for real.
"""

from __future__ import annotations

import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from app import diagnostics as diag
from app.config import settings
from app.diagnostics import (
    LOG_FIELD_WHITELIST,
    SafeStreamHandler,
    StructuredJsonFormatter,
    log_diagnostic,
    scrub_record,
    setup_diagnostics,
    shutdown_diagnostics,
)


@pytest.fixture
def diagnostics_state():
    """No diagnostics handlers or flags may leak between tests (same reset as shutdown)."""
    shutdown_diagnostics()
    yield
    shutdown_diagnostics()


def _open_console_handlers(root_logger: logging.Logger):
    """Handlers `setup_diagnostics` itself counts as an installed console handler."""
    return [
        h
        for h in root_logger.handlers
        if isinstance(h, logging.StreamHandler)
        and not isinstance(h, RotatingFileHandler)
        and diag._handler_is_open(h)
    ]


def _detach_console_handlers(root_logger: logging.Logger):
    """Temporarily removes every non-file stream handler (e.g. pytest's capture)."""
    removed = [
        h
        for h in list(root_logger.handlers)
        if isinstance(h, logging.StreamHandler)
        and not isinstance(h, RotatingFileHandler)
    ]
    for handler in removed:
        root_logger.removeHandler(handler)
    return removed


def _read_entries(log_dir: Path):
    target = log_dir / "backend.jsonl"
    if not target.exists():
        return []
    return [
        json.loads(line)
        for line in target.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# --------------------------------------------------------------------------------------
# Degraded init: the log target cannot be created, startup must continue anyway
# --------------------------------------------------------------------------------------

def test_log_dir_occupied_by_regular_file_does_not_abort_startup(
    tmp_path, monkeypatch, capsys, diagnostics_state
):
    """
    A regular file occupies the `logs` target -> `settings.log_path`'s mkdir raises
    FileExistsError on attribute access. Previously this propagated out of
    `setup_diagnostics()` (it ran before the try block) and crashed the module-level
    call in app/main.py.
    """
    occupied = tmp_path / "logs"
    occupied.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(settings, "LOG_DIR", str(occupied))

    # Strip pytest's own capture handler so we can prove OUR console handler installs.
    root_logger = logging.getLogger()
    detached = _detach_console_handlers(root_logger)
    try:
        setup_diagnostics()  # must not raise

        assert isinstance(diag._diagnostics_console_handler, SafeStreamHandler)
        assert diag._diagnostics_console_handler in root_logger.handlers
    finally:
        for handler in detached:
            if handler not in root_logger.handlers:
                root_logger.addHandler(handler)

    err = capsys.readouterr().err
    assert "[DIAGNOSTICS INIT ERROR]" in err
    assert "Cannot initialize log file" in err
    assert diag._diagnostics_initialized is True, (
        "degraded is not terminal: init still completes so console logging is live"
    )
    assert diag._diagnostics_file_handler is None, "no file handler was attached"
    # The occupying file must be left exactly as it was — never deleted or replaced.
    assert occupied.read_text(encoding="utf-8") == "not a directory"


def test_log_dir_below_regular_file_does_not_abort_startup(
    tmp_path, monkeypatch, capsys, diagnostics_state
):
    """
    An intermediate path component is a regular file -> mkdir raises NotADirectoryError
    (deterministic on POSIX AND Windows, unlike ACL tricks). Same degraded contract.
    """
    blocked_parent = tmp_path / "occupied"
    blocked_parent.write_text("plain file", encoding="utf-8")
    monkeypatch.setattr(settings, "LOG_DIR", str(blocked_parent / "logs"))

    setup_diagnostics()  # must not raise

    assert "[DIAGNOSTICS INIT ERROR]" in capsys.readouterr().err
    assert diag._diagnostics_initialized is True
    assert diag._diagnostics_file_handler is None
    assert _open_console_handlers(logging.getLogger()), (
        "a usable console handler must remain on the root logger"
    )


def test_log_dir_under_unwritable_parent_does_not_abort_startup(
    tmp_path, monkeypatch, capsys, diagnostics_state
):
    """
    ACL/mode refusal: the parent is chmod 0o000 so the mkdir inside `log_path` raises
    PermissionError. Where mode bits are not enforced (root, Windows ACL semantics on
    CI) this skips — the file-occupation cases above cover the identical code path.
    """
    locked = tmp_path / "locked"
    locked.mkdir()
    target = locked / "logs"
    os.chmod(locked, 0o000)
    try:
        if os.access(locked, os.W_OK | os.X_OK):
            pytest.skip(
                "mode bits not enforced here (root / Windows ACL semantics); "
                "the file-occupation tests cover the same degraded code path"
            )
        monkeypatch.setattr(settings, "LOG_DIR", str(target))

        setup_diagnostics()  # must not raise

        assert "[DIAGNOSTICS INIT ERROR]" in capsys.readouterr().err
        assert diag._diagnostics_initialized is True
        assert diag._diagnostics_file_handler is None
        assert _open_console_handlers(logging.getLogger())
    finally:
        os.chmod(locked, 0o755)


def test_setup_retries_file_handler_after_degraded_init(
    tmp_path, monkeypatch, capsys, diagnostics_state
):
    """
    A failed init must not be terminal: `_handler_is_open(None)` is False, so the
    idempotence guard lets a later call retry once the log path works again.
    """
    occupied = tmp_path / "logs"
    occupied.write_text("plain file", encoding="utf-8")
    monkeypatch.setattr(settings, "LOG_DIR", str(occupied))

    setup_diagnostics()
    assert diag._diagnostics_file_handler is None
    capsys.readouterr()  # drain the first [DIAGNOSTICS INIT ERROR]

    healthy = tmp_path / "logs-ok"
    monkeypatch.setattr(settings, "LOG_DIR", str(healthy))
    setup_diagnostics()  # retry now succeeds — degraded state was not latched

    err = capsys.readouterr().err
    assert "[DIAGNOSTICS INIT ERROR]" not in err, "the retry must not report a new error"
    assert diag._diagnostics_file_handler is not None
    assert Path(diag._diagnostics_file_handler.baseFilename) == (
        healthy.resolve() / "backend.jsonl"
    )

    log_diagnostic("startup.ready", message="重试后日志恢复", phase="ready")
    shutdown_diagnostics()

    events = [entry.get("event") for entry in _read_entries(healthy)]
    assert "startup.ready" in events


# --------------------------------------------------------------------------------------
# Regression: a healthy LOG_DIR still produces a working file handler
# --------------------------------------------------------------------------------------

def test_log_dir_normal_path_creates_file_handler_and_writes(
    tmp_path, monkeypatch, diagnostics_state
):
    """LOG_DIR -> `settings.log_path` mkdirs it on access; records must reach the file."""
    target = tmp_path / "logs"  # deliberately not pre-created: log_path must mkdir it
    monkeypatch.setattr(settings, "LOG_DIR", str(target))

    setup_diagnostics()

    assert diag._diagnostics_initialized is True
    assert diag._diagnostics_file_handler is not None
    assert Path(diag._diagnostics_file_handler.baseFilename) == (
        target.resolve() / "backend.jsonl"
    )

    log_diagnostic("startup.ready", message="日志系统已就绪", phase="ready")
    shutdown_diagnostics()

    entries = _read_entries(target)
    assert entries, "the record must have reached backend.jsonl via the real default path"
    entry = entries[-1]
    assert entry["event"] == "startup.ready"
    assert entry["message"] == "日志系统已就绪"
    assert entry["source"] == "backend"


# --------------------------------------------------------------------------------------
# Whitelist mirror: desktop-minted log_record_id (beta.3 D2) must survive the contract
# --------------------------------------------------------------------------------------

def test_log_record_id_is_whitelisted_mirror_of_desktop():
    """
    The backend never mints `log_record_id`, but records relayed from the desktop carry
    it; the whitelist mirror must keep the field or the shared schema silently drops it.
    """
    assert "log_record_id" in LOG_FIELD_WHITELIST

    scrubbed = scrub_record(
        {
            "event": "log.persistence.recovered",
            "log_record_id": "sess-deadbeef:desktop:00000042",
            "entries": 3,
            "dropped": 0,
        }
    )
    assert scrubbed["log_record_id"] == "sess-deadbeef:desktop:00000042"

    # The formatter's whitelist-driven pass-through must emit it on the wire too.
    record = logging.getLogger("app.test").makeRecord(
        "app.test", logging.INFO, __file__, 1, "补写完成", (), None
    )
    record.event = "log.persistence.recovered"
    record.log_record_id = "sess-deadbeef:desktop:00000042"
    entry = json.loads(StructuredJsonFormatter().format(record))
    assert entry["log_record_id"] == "sess-deadbeef:desktop:00000042"
    assert entry["event"] == "log.persistence.recovered"
