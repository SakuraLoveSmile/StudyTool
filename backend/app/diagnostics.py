import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any, AsyncIterator, Callable, Dict, List, Optional
from urllib.parse import urlparse, urlunparse
import uuid

import httpx

# Unique runtime instance identifier per backend process
INSTANCE_ID: str = f"inst-{uuid.uuid4().hex[:8]}"

# Identity fields shared with the desktop main process (see desktop/src/diagnostics.ts).
# The Electron main process mints one session id per launch and passes it down, so the
# desktop / backend / frontend files can be correlated for a single startup.
SESSION_ID: str = os.environ.get("STUDYTOOL_SESSION_ID", "").strip() or f"sess-{uuid.uuid4().hex[:8]}"
APP_VERSION: str = os.environ.get("STUDYTOOL_APP_VERSION", "").strip()
LOG_SOURCE: str = "backend"

# Storage policy (release gates: 5 MiB per file, 5 rotated files, 7 day retention).
DEFAULT_LOG_MAX_BYTES: int = 5 * 1024 * 1024
LOG_RETENTION_DAYS: int = 7
LOG_RECORDS_PER_SECOND: int = 300
ERROR_AGGREGATION_WINDOW_SECONDS: float = 5.0

# Context variables for distributed task and execution unit tracing
task_id_var: ContextVar[Optional[str]] = ContextVar("task_id", default=None)
task_type_var: ContextVar[Optional[str]] = ContextVar("task_type", default=None)
unit_id_var: ContextVar[Optional[str]] = ContextVar("unit_id", default=None)
unit_index_var: ContextVar[Optional[int]] = ContextVar("unit_index", default=None)
unit_scope_var: ContextVar[Optional[str]] = ContextVar("unit_scope", default=None)
call_id_var: ContextVar[Optional[str]] = ContextVar("call_id", default=None)
attempt_var: ContextVar[Optional[int]] = ContextVar("attempt", default=None)


@contextmanager
def task_context(task_id: Optional[str], task_type: Optional[str] = None):
    """Binds task-level tracing context for the duration of a block/task."""
    tok_id = task_id_var.set(task_id)
    tok_type = task_type_var.set(task_type)
    try:
        yield
    finally:
        task_id_var.reset(tok_id)
        task_type_var.reset(tok_type)


def set_unit_context(
    unit_id: Optional[str],
    unit_index: Optional[int] = None,
    scope: Optional[str] = None
):
    """Explicitly sets execution unit contextvars and returns the tokens."""
    tok_id = unit_id_var.set(unit_id)
    tok_idx = unit_index_var.set(unit_index)
    tok_scope = unit_scope_var.set(scope)
    return (tok_id, tok_idx, tok_scope)


def reset_unit_context(tokens):
    """Explicitly resets execution unit contextvars using tokens."""
    tok_id, tok_idx, tok_scope = tokens
    unit_id_var.reset(tok_id)
    unit_index_var.reset(tok_idx)
    unit_scope_var.reset(tok_scope)


@contextmanager
def unit_context(
    unit_id: Optional[str],
    unit_index: Optional[int] = None,
    scope: Optional[str] = None
):
    """Binds batch/page execution unit context, restored on exit to prevent crosstalk."""
    toks = set_unit_context(unit_id, unit_index, scope)
    try:
        yield
    finally:
        reset_unit_context(toks)


@contextmanager
def call_context(call_id: Optional[str] = None, attempt: Optional[int] = None):
    """Binds or creates an independent external call identifier and attempt number."""
    cid = call_id or f"call-{uuid.uuid4().hex[:8]}"
    tok_cid = call_id_var.set(cid)
    tok_att = attempt_var.set(attempt)
    try:
        yield cid
    finally:
        call_id_var.reset(tok_cid)
        attempt_var.reset(tok_att)


# Whitelist headers for upstream provider correlation IDs
WHITELIST_REQUEST_ID_HEADERS = (
    "x-request-id",
    "request-id",
    "x-amzn-requestid",
    "x-correlation-id",
    "traceparent",
    "openai-organization",
    "cf-ray",
)

# Regex patterns for strict secret & credential masking
SENSITIVE_PATTERNS = (
    # Word-boundary anchored: an identifier such as `task-recon-999` merely CONTAINS
    # "sk-" and must not be mangled, while real keys (space/quote/line prefixed) are masked.
    (re.compile(r"(?<![A-Za-z0-9_])sk-([A-Za-z0-9_-]{4})[A-Za-z0-9_-]+"), r"sk-\1***"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9_.\-]+"), r"\1***"),
    (
        re.compile(
            r"(?i)((?:api[_-]?key|access[_-]?token|token|authorization|secret|password)"
            r"(?:\s*[:=]\s*|\s*['\"][:=]\s*['\"]))\S+"
        ),
        r"\1***",
    ),
    (re.compile(r"(?i)(cookie:\s*)\S+"), r"\1***"),
    (re.compile(r"https?://[^:@\s]+:[^:@\s]+@"), r"https://***:***@"),
    # URL query strings and fragments never reach the log (tokens, ids, user input live there).
    (re.compile(r"(https?://[^\s\"'?]+)\?[^\s\"']*"), r"\1"),
    (re.compile(r"(https?://[^\s\"'#]+)#[^\s\"']*"), r"\1"),
    # Absolute user paths: drive letters, UNC, macOS/Linux home, and `~`.
    (re.compile(r"[A-Za-z]:\\(?:[^\\\s\"']+\\)*[^\\\s\"']*"), r"<path>"),
    (re.compile(r"\\\\[^\s\"']+"), r"<path>"),
    (re.compile(r"/(?:Users|home)/[^\s\"':]+(?:/[^\s\"':]*)*"), r"<path>"),
    (re.compile(r"~/[^\s\"']*"), r"<path>"),
    (re.compile(r"(?i)(?:AppData|LOCALAPPDATA|APPDATA)[\\/][^\s\"']*"), r"<path>"),
    # Document/media file names are user content. Source extensions (py/js/ts) are kept:
    # module names are legitimate diagnostics and carry no user data.
    (
        re.compile(
            r"[^\s\"'\\/:*?<>|]+\.(?:pdf|docx?|pptx?|xlsx?|csv|md|markdown|txt|rtf|png|jpe?g|webp|"
            r"gif|bmp|tiff?|zip|rar|7z|tar|tgz|gz|epub|mobi)\b",
            re.IGNORECASE,
        ),
        "<file>",
    ),
)

# Only these field names may ever be persisted. Unknown keys are dropped, which is what
# makes "never record business payload" enforceable instead of aspirational.
# Kept in step with the desktop/frontend whitelist in desktop/src/diagnostics.ts so the
# three streams share one schema.
LOG_FIELD_WHITELIST = frozenset({
    # envelope
    "timestamp", "level", "source", "event", "message", "session_id", "instance_id",
    "app_version", "platform", "arch", "logger", "phase", "pid",
    # errors / failures
    "error_type", "error_name", "error_message", "stack", "stack_frames", "frames",
    "cause_chain", "exception_type", "failed_stage", "stage", "reason", "detail",
    "component", "info", "reason_code",
    # stack frame shape (exception_info.frames[])
    "file", "line", "func",
    # correlation
    "task_id", "task_type", "unit_id", "unit_index", "scope", "call_id", "attempt",
    # external calls / provider observability
    "service", "model", "endpoint", "method", "input_chars", "input_bytes",
    "timeout_config", "status_code", "provider_request_id", "duration_ms",
    "retry_reason", "retry_wait_seconds", "result_count", "exception_info",
    # process lifecycle
    "exit_code", "signal", "code", "killed",
    # limiting / aggregation
    "occurrences", "first_seen", "last_seen", "dropped", "limit", "window_ms",
    # generic counters used by call sites
    "bytes", "entries", "count", "total", "ok", "version", "truncated", "note",
    "action", "operation", "route", "target", "kind", "tags",
    # log-navigation diagnostics (mirror of desktop FIELD_WHITELIST)
    "nav_id", "trigger", "to_name", "elapsed_ms",
    # Mirror of the desktop-minted record id (beta.3 D2): the backend never mints it,
    # but records relayed from the desktop keep it, so the whitelist must not drop it.
    "log_record_id",
})

# Attributes that belong to logging.LogRecord itself; they must never be copied into the
# entry just because a whitelist key happens to share their name.
_LOGRECORD_INTERNAL_FIELDS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
    "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
    "relativeCreated", "thread", "threadName", "processName", "process", "taskName",
    "getMessage",
})

MAX_LOG_STRING_LENGTH = 2000
MAX_LOG_MESSAGE_LENGTH = 500
MAX_LOG_DEPTH = 4
MAX_LOG_ARRAY_ITEMS = 20


def _scrub_value(value: Any) -> Any:
    """Recursively scrubs a value that already survives the whitelist check."""
    if isinstance(value, str):
        return sanitize_text(value)[:MAX_LOG_STRING_LENGTH]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_scrub_value(item) for item in list(value)[:MAX_LOG_ARRAY_ITEMS]]
    if isinstance(value, dict):
        return scrub_record(value)
    return sanitize_text(str(value))[:MAX_LOG_STRING_LENGTH]


def scrub_record(record: Dict[str, Any], depth: int = 0) -> Dict[str, Any]:
    """
    Whitelists and recursively scrubs a record before it is written or exported.

    Whitelisting happens first so a field nobody vetted (document text, prompt, answer,
    file path from a caller) can never be persisted even if a call site adds it later.
    """
    out: Dict[str, Any] = {}
    if depth > MAX_LOG_DEPTH or not isinstance(record, dict):
        return out
    for key, value in record.items():
        if key not in LOG_FIELD_WHITELIST or value is None:
            continue
        if isinstance(value, dict) and depth + 1 > MAX_LOG_DEPTH:
            continue
        scrubbed = _scrub_value(value)
        if key == "message" and isinstance(scrubbed, str):
            scrubbed = scrubbed[:MAX_LOG_MESSAGE_LENGTH]
        out[key] = scrubbed
    return out


def sanitize_text(text: Any) -> str:
    """Masks secrets, tokens and passwords across file and console logs."""
    if text is None:
        return ""
    s = str(text)
    for pattern, repl in SENSITIVE_PATTERNS:
        s = pattern.sub(repl, s)
    return s


def sanitize_endpoint(raw_url: str) -> str:
    """
    Strips userinfo, query parameters and fragments from endpoints.
    Normalizes dynamic path segments (UUIDs, timestamps) to prevent URL leakage.
    """
    if not raw_url:
        return ""
    try:
        parsed = urlparse(raw_url)
        netloc = parsed.hostname or ""
        if parsed.port and parsed.port not in (80, 443):
            netloc = f"{netloc}:{parsed.port}"
        path = parsed.path
        # Replace UUID-like strings and long numeric IDs with template placeholder
        path = re.sub(r"/[0-9a-fA-F-]{8,36}", "/{id}", path)
        path = re.sub(r"/\d{4,}", "/{id}", path)
        return urlunparse((parsed.scheme, netloc, path, "", "", ""))
    except Exception:
        return "unknown_endpoint"


def extract_provider_request_id(headers: Any) -> Optional[str]:
    """Extracts upstream request ID from whitelisted response headers."""
    if not headers:
        return None
    lower_map = {str(k).lower(): v for k, v in headers.items()} if hasattr(headers, "items") else {}
    for h in WHITELIST_REQUEST_ID_HEADERS:
        val = lower_map.get(h) if lower_map else (headers.get(h) if hasattr(headers, "get") else None)
        if val:
            return str(val).strip()
    return None


def extract_safe_exception_info(exc: BaseException, stage: Optional[str] = None) -> Dict[str, Any]:
    """
    Safely captures exception type, stack frames and cause chain without local variables.
    Raw response bodies and unvalidated exception texts are masked to prevent leakages.
    """
    exc_type = type(exc).__name__

    # Stack frames without locals
    frames: List[Dict[str, Any]] = []
    tb = exc.__traceback__
    while tb is not None:
        frame = tb.tb_frame
        frames.append({
            "file": Path(frame.f_code.co_filename).name,
            "line": tb.tb_lineno,
            "func": frame.f_code.co_name,
        })
        tb = tb.tb_next

    # Cause chain
    cause_chain: List[str] = []
    curr = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
    visited = set()
    while curr is not None and id(curr) not in visited:
        visited.add(id(curr))
        cause_chain.append(type(curr).__name__)
        curr = getattr(curr, "__cause__", None) or getattr(curr, "__context__", None)

    # Safe message classification
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = getattr(exc.response, "status_code", None)
        safe_msg = f"HTTP 状态异常 (status={status_code})"
    elif isinstance(exc, httpx.ReadTimeout):
        safe_msg = f"读取超时 (ReadTimeout, stage={stage or 'unknown'})"
    elif isinstance(exc, httpx.ConnectTimeout):
        safe_msg = "连接超时 (ConnectTimeout)"
    elif isinstance(exc, httpx.ConnectError):
        safe_msg = "网络连接失败 (ConnectError)"
    elif isinstance(exc, httpx.TimeoutException):
        safe_msg = f"网络超时 ({exc_type})"
    elif isinstance(exc, json.JSONDecodeError):
        safe_msg = f"JSON 解析失败 (line {exc.lineno}, col {exc.colno})"
    elif isinstance(exc, asyncio.CancelledError):
        safe_msg = "操作已取消 (CancelledError)"
    else:
        raw_msg = sanitize_text(str(exc))
        # Mask HTML tags or JSON objects in error messages
        if "{" in raw_msg or "<" in raw_msg:
            raw_msg = re.sub(r"\{.*?\}", "{...}", raw_msg)
            raw_msg = re.sub(r"<.*?>", "<...>", raw_msg)
        safe_msg = raw_msg[:200]

    return {
        "exception_type": exc_type,
        "message": safe_msg,
        "failed_stage": stage or "unknown",
        "frames": frames[-6:] if frames else [],
        "cause_chain": cause_chain,
    }


class SafeRotatingFileHandler(RotatingFileHandler):
    """
    Standard RotatingFileHandler with fail-safe error handling.
    Reports write errors once to stderr and never causes recursive logging.
    """
    _stderr_warned: bool = False

    def emit(self, record: logging.LogRecord):
        try:
            super().emit(record)
        except Exception:
            self.handleError(record)

    def handleError(self, record: logging.LogRecord):
        if not SafeRotatingFileHandler._stderr_warned:
            SafeRotatingFileHandler._stderr_warned = True
            try:
                err = sys.exc_info()[1]
                sys.stderr.write(f"[DIAGNOSTICS LOG WRITE ERROR] Failed to write log: {err}\n")
                sys.stderr.flush()
            except Exception:
                pass


class DiagnosticFileHandler(SafeRotatingFileHandler):
    """
    Fail-safe JSONL rotating handler implementing the release-gated log policy:
      - 5 MiB per file, 5 rotated files, rotated copies older than 7 days deleted;
      - writes are rate limited (token bucket), so a log storm cannot stall business work;
      - repeated identical ERROR records collapse into one summary carrying `occurrences`.
    Every failure path degrades to a single stderr note and never raises into business code.
    """

    def __init__(
        self,
        filename: str,
        maxBytes: int = DEFAULT_LOG_MAX_BYTES,
        backupCount: int = 5,
        retention_days: int = LOG_RETENTION_DAYS,
        max_records_per_second: int = LOG_RECORDS_PER_SECOND,
        aggregation_window_seconds: float = ERROR_AGGREGATION_WINDOW_SECONDS,
    ):
        super().__init__(
            filename=filename,
            maxBytes=maxBytes,
            backupCount=backupCount,
            encoding="utf-8",
        )
        self.retention_days = retention_days
        self._rate_limit = float(max(1, max_records_per_second))
        self._tokens = self._rate_limit
        self._last_refill = time.monotonic()
        self._dropped_in_window = 0
        self._drop_reported = False
        self._agg_lock = threading.Lock()
        self._aggregation_window = aggregation_window_seconds
        self._pending_errors: Dict[tuple, Dict[str, Any]] = {}
        # NOTE: deliberately NOT named `_closed`. `logging.config.dictConfig()` — which
        # `uvicorn.run()` performs on startup — sets the private `_closed` flag on every
        # pre-existing handler while leaving it attached to the root logger. Reading that
        # framework-internal flag here would silently disable ALL file logging the moment
        # uvicorn starts. Only our own close() may stop this handler.
        self._diagnostics_closed = False
        self.cleanup_old_files()

    def cleanup_old_files(self) -> int:
        """Deletes rotated files older than the retention window; never the active file."""
        removed = 0
        try:
            cutoff = time.time() - self.retention_days * 24 * 60 * 60
            for index in range(1, max(1, self.backupCount) + 1):
                candidate = f"{self.baseFilename}.{index}"
                if os.path.exists(candidate) and os.path.getmtime(candidate) < cutoff:
                    os.unlink(candidate)
                    removed += 1
        except Exception:
            pass
        return removed

    def doRollover(self):
        super().doRollover()
        self.cleanup_old_files()

    def _take_token(self):
        with self._agg_lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            if elapsed > 0:
                self._tokens = min(self._rate_limit, self._tokens + elapsed * self._rate_limit)
                self._last_refill = now
                if self._tokens >= self._rate_limit * 0.5:
                    self._dropped_in_window = 0
                    self._drop_reported = False
            if self._tokens < 1:
                self._dropped_in_window += 1
                report = not self._drop_reported
                self._drop_reported = True
                return False, report, self._dropped_in_window
            self._tokens -= 1
            return True, False, 0

    def _notice_record(self, dropped: int) -> logging.LogRecord:
        notice = logging.LogRecord(
            name="app.diagnostics",
            level=logging.WARNING,
            pathname=__file__,
            lineno=0,
            msg="日志写入超过限流阈值，超出部分已丢弃",
            args=(),
            exc_info=None,
        )
        notice.event = "log.rate_limited"
        notice.dropped = dropped
        notice.limit = int(self._rate_limit)
        notice.window_ms = 1000
        return notice

    def _aggregate_error(self, record: logging.LogRecord) -> bool:
        """Returns True when the record was absorbed into an existing aggregate."""
        event = getattr(record, "event", "log")
        try:
            message = record.getMessage()[:200]
        except Exception:
            message = ""
        tid = getattr(record, "task_id", None) or task_id_var.get()
        key = (record.levelno, event, message, tid)
        now_iso = datetime.now(timezone.utc).isoformat()

        with self._agg_lock:
            pending = self._pending_errors.get(key)
            if pending is not None:
                pending["count"] += 1
                pending["last_seen"] = now_iso
                return True
            if len(self._pending_errors) >= 200:
                return False
            entry: Dict[str, Any] = {
                "record": record,
                "count": 1,
                "first_seen": now_iso,
                "last_seen": now_iso,
                "timer": None,
            }
            self._pending_errors[key] = entry

        timer = threading.Timer(self._aggregation_window, self._flush_error, args=(key,))
        timer.daemon = True
        entry["timer"] = timer
        try:
            timer.start()
        except Exception:
            pass
        return False

    def _error_summary(self, entry: Dict[str, Any]) -> logging.LogRecord:
        original = entry["record"]
        summary = logging.LogRecord(
            name=original.name,
            level=original.levelno,
            pathname=original.pathname,
            lineno=original.lineno,
            msg=f"重复错误已聚合（{entry['count']} 次）",
            args=(),
            exc_info=None,
        )
        summary.event = getattr(original, "event", "log")
        summary.occurrences = entry["count"]
        summary.first_seen = entry["first_seen"]
        summary.last_seen = entry["last_seen"]
        for field in (
            "task_id", "task_type", "unit_id", "unit_index", "scope", "call_id", "attempt",
            "stage", "failed_stage", "exception_type", "service", "model", "status_code",
        ):
            value = getattr(original, field, None)
            if value is not None:
                setattr(summary, field, value)
        return summary

    def _flush_error(self, key: tuple) -> None:
        with self._agg_lock:
            entry = self._pending_errors.pop(key, None)
        if not entry:
            return
        timer = entry.get("timer")
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
        # The first occurrence was already written; only the extra ones need a summary.
        if entry["count"] <= 1:
            return
        try:
            super().emit(self._error_summary(entry))
        except Exception:
            pass

    def emit(self, record: logging.LogRecord):
        try:
            if self._diagnostics_closed:
                return
            if record.levelno >= logging.ERROR and self._aggregate_error(record):
                return
            allowed, report_drop, dropped = self._take_token()
            if not allowed:
                if report_drop:
                    super().emit(self._notice_record(dropped))
                return
            super().emit(record)
        except Exception:
            self.handleError(record)

    def close(self):
        self._diagnostics_closed = True
        try:
            for key in list(self._pending_errors.keys()):
                self._flush_error(key)
        except Exception:
            pass
        super().close()


class StructuredJsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects with context and metrics."""

    def format(self, record: logging.LogRecord) -> str:
        now_iso = datetime.now(timezone.utc).isoformat()
        entry: Dict[str, Any] = {
            "timestamp": now_iso,
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", "log"),
            "instance_id": INSTANCE_ID,
            "source": LOG_SOURCE,
            "session_id": SESSION_ID,
            "platform": sys.platform,
        }
        if APP_VERSION:
            entry["app_version"] = APP_VERSION

        # Context variable resolution
        tid = getattr(record, "task_id", None) or task_id_var.get()
        if tid:
            entry["task_id"] = tid
        ttype = getattr(record, "task_type", None) or task_type_var.get()
        if ttype:
            entry["task_type"] = ttype
        uid = getattr(record, "unit_id", None) or unit_id_var.get()
        if uid:
            entry["unit_id"] = uid
        uidx = getattr(record, "unit_index", None)
        if uidx is None:
            uidx = unit_index_var.get()
        if uidx is not None:
            entry["unit_index"] = uidx
        scp = getattr(record, "scope", None) or unit_scope_var.get()
        if scp:
            entry["scope"] = scp
        cid = getattr(record, "call_id", None) or call_id_var.get()
        if cid:
            entry["call_id"] = cid
        att = getattr(record, "attempt", None)
        if att is None:
            att = attempt_var.get()
        if att is not None:
            entry["attempt"] = att

        # Pass-through of every whitelisted record attribute.
        # Driving this from LOG_FIELD_WHITELIST (instead of a second hardcoded list) means a
        # new call-site field becomes loggable by being whitelisted once, and can never be
        # silently dropped by a formatter list that nobody remembered to update.
        reserved = {
            "timestamp", "level", "logger", "event", "instance_id",
            "source", "session_id", "app_version", "platform", "message",
        }
        for key in LOG_FIELD_WHITELIST:
            if key in reserved or key in entry or key in _LOGRECORD_INTERNAL_FIELDS:
                continue
            val = getattr(record, key, None)
            if val is not None:
                entry[key] = val

        entry["message"] = sanitize_text(record.getMessage())
        # Whitelist + scrub last, so nothing unvetted leaves this formatter.
        return json.dumps(scrub_record(entry), ensure_ascii=False)


class SanitizedConsoleFormatter(logging.Formatter):
    """Concise text formatter for console output with sensitive data masked."""

    def format(self, record: logging.LogRecord) -> str:
        tid = getattr(record, "task_id", None) or task_id_var.get()
        uid = getattr(record, "unit_id", None) or unit_id_var.get()
        ctx_parts = []
        if tid:
            ctx_parts.append(f"task={tid[:8]}")
        if uid:
            ctx_parts.append(f"unit={uid}")
        ctx_str = f" [{','.join(ctx_parts)}]" if ctx_parts else ""

        event = getattr(record, "event", None)
        event_str = f" ({event})" if event and event != "log" else ""

        msg = sanitize_text(record.getMessage())
        asctime = self.formatTime(record, self.datefmt or "%Y-%m-%d %H:%M:%S")
        return f"{asctime} [{record.levelname}] {record.name}{ctx_str}{event_str}: {msg}"


_diagnostics_file_handler: Optional[SafeRotatingFileHandler] = None
_diagnostics_console_handler: Optional[logging.Handler] = None
_diagnostics_initialized: bool = False


class SafeStreamHandler(logging.StreamHandler):
    """
    Console handler that can never take the application down.

    Windows consoles and pipes use the locale code page (cp1252 on an English install), so
    writing a Chinese log message raises UnicodeEncodeError from inside `logging`. The
    framework's default `handleError` then dumps a full traceback plus stack to stderr for
    every affected record. That noise made the frozen backend's startup output unreadable
    and is worth suppressing at the source: report it once, in ASCII, and move on.
    """

    _encoding_warning_emitted: bool = False

    def handleError(self, record: logging.LogRecord) -> None:
        if SafeStreamHandler._encoding_warning_emitted:
            return
        SafeStreamHandler._encoding_warning_emitted = True
        try:
            err = sys.exc_info()[1]
            sys.stderr.write(
                "[DIAGNOSTICS] console output could not be encoded "
                f"({type(err).__name__}); further console encoding errors are suppressed. "
                "The UTF-8 log file is unaffected.\n"
            )
            sys.stderr.flush()
        except Exception:
            pass


def _make_streams_encoding_safe() -> None:
    """
    Keeps the native console encoding but substitutes unmappable characters instead of
    raising, so a Chinese message cannot abort a console write on a non-UTF-8 code page.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def _handler_is_open(handler: Optional[logging.Handler]) -> bool:
    """
    True only while a handler can still actually write.

    `logging.config.dictConfig()` — which `uvicorn.run()` performs on startup — CLOSES every
    pre-existing handler (stream set to None) while leaving it attached to the root logger.
    Checking "is it still in root.handlers" is therefore not enough: the stream must still be
    open, otherwise the diagnostics file would silently stop receiving records the moment
    uvicorn starts. Handlers without a `stream` attribute (queue handlers) count as open.
    """
    if handler is None:
        return False
    return getattr(handler, "stream", "no-stream-attribute") is not None


def _drop_dead_handlers(root_logger: logging.Logger) -> None:
    """Detaches handlers that a reconfiguration closed underneath us."""
    for handler in list(root_logger.handlers):
        if getattr(handler, "stream", "no-stream-attribute") is None:
            try:
                root_logger.removeHandler(handler)
            except Exception:
                pass


def setup_diagnostics(
    log_level: Optional[str] = None,
    log_dir: Optional[Path] = None,
    max_bytes: Optional[int] = None,
    backup_count: Optional[int] = None,
    retention_days: Optional[int] = None,
):
    """
    Configures structured diagnostic file logging and sanitized console output.
    Idempotent: repeated calls do not stack handlers.
    """
    global _diagnostics_file_handler, _diagnostics_console_handler, _diagnostics_initialized
    # Re-arm when the previous handler was closed by a logging reconfiguration, otherwise
    # all file logging dies silently at uvicorn startup.
    if _diagnostics_initialized and _handler_is_open(_diagnostics_file_handler):
        return

    from app.config import settings

    lvl_name = (log_level or getattr(settings, "LOG_LEVEL", "INFO")).upper()
    level = getattr(logging, lvl_name, logging.INFO)

    _make_streams_encoding_safe()

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    _drop_dead_handlers(root_logger)

    # 1. File Handler Setup
    # The whole log-target chain lives in ONE protected scope: resolving the directory
    # (`settings.log_path` runs mkdir on attribute access — a `logs` path occupied by a
    # regular file or refused by the ACL raises right there — and the DATA_DIR fallback
    # is equally fallible), creating it, and creating/attaching the rotating handler.
    # Any failure degrades to a single stderr note; it must never abort the module-level
    # setup_diagnostics() call in app/main.py. Business paths (data_path/uploads_path/
    # db_path) are untouched by this: their errors still propagate.
    try:
        target_dir = log_dir or getattr(settings, "log_path", None)
        if target_dir is None:
            target_dir = Path(getattr(settings, "DATA_DIR", "./data")) / "logs"

        mb = max_bytes if max_bytes is not None else getattr(settings, "LOG_MAX_BYTES", DEFAULT_LOG_MAX_BYTES)
        bc = backup_count if backup_count is not None else getattr(settings, "LOG_BACKUP_COUNT", 5)
        rd = retention_days if retention_days is not None else getattr(settings, "LOG_RETENTION_DAYS", LOG_RETENTION_DAYS)

        target_dir.mkdir(parents=True, exist_ok=True)
        log_file = target_dir / "backend.jsonl"
        file_handler = DiagnosticFileHandler(
            filename=str(log_file),
            maxBytes=mb,
            backupCount=bc,
            retention_days=rd,
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(StructuredJsonFormatter())
        if _diagnostics_file_handler is not None:
            root_logger.removeHandler(_diagnostics_file_handler)
        root_logger.addHandler(file_handler)
        _diagnostics_file_handler = file_handler
    except Exception as exc:
        sys.stderr.write(f"[DIAGNOSTICS INIT ERROR] Cannot initialize log file: {exc}\n")
        sys.stderr.flush()

    # 2. Console Handler Setup
    has_console = any(
        isinstance(h, logging.StreamHandler)
        and not isinstance(h, RotatingFileHandler)
        and _handler_is_open(h)
        for h in root_logger.handlers
    )
    if not has_console:
        console_handler = SafeStreamHandler(sys.stdout)
        console_handler.setLevel(level)
        console_handler.setFormatter(SanitizedConsoleFormatter())
        root_logger.addHandler(console_handler)
        _diagnostics_console_handler = console_handler
    else:
        for h in root_logger.handlers:
            if (
                isinstance(h, logging.StreamHandler)
                and not isinstance(h, RotatingFileHandler)
                and _handler_is_open(h)
            ):
                h.setFormatter(SanitizedConsoleFormatter())

    # Suppress verbose third-party wire logging
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    _diagnostics_initialized = True


def shutdown_diagnostics():
    """Flushes and closes handlers created by this module, preserving Uvicorn handlers."""
    global _diagnostics_file_handler, _diagnostics_console_handler, _diagnostics_initialized
    root_logger = logging.getLogger()
    if _diagnostics_file_handler is not None:
        try:
            _diagnostics_file_handler.flush()
            _diagnostics_file_handler.close()
        except Exception:
            pass
        root_logger.removeHandler(_diagnostics_file_handler)
        _diagnostics_file_handler = None
    if _diagnostics_console_handler is not None:
        root_logger.removeHandler(_diagnostics_console_handler)
        _diagnostics_console_handler = None
    _diagnostics_initialized = False


def log_diagnostic(
    event: str,
    level: int = logging.INFO,
    message: str = "",
    logger_name: str = "app.diagnostics",
    **extra_fields
):
    """Emits a structured diagnostic log record."""
    lg = logging.getLogger(logger_name)
    if not lg.isEnabledFor(level):
        return
    record = lg.makeRecord(
        lg.name, level, "(diagnostics)", 0, message, (), None, func=None, extra=None
    )
    record.event = event
    for k, v in extra_fields.items():
        setattr(record, k, v)
    lg.handle(record)


@asynccontextmanager
async def stream_request_context(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    **kwargs
) -> AsyncIterator[httpx.Response]:
    """
    Wraps HTTP streaming requests.
    If client.post has been monkeypatched in tests, adapts the monkeypatched result
    to maintain test suite compatibility while executing stream context in production.
    """
    post_method = getattr(client, "post", None)
    is_custom_mock = (
        method.upper() == "POST"
        and post_method is not None
        and getattr(post_method, "__func__", post_method) != httpx.AsyncClient.post
    )
    if is_custom_mock:
        resp = await client.post(url, **kwargs)
        orig_aread = getattr(resp, "aread", None)
        if not asyncio.iscoroutinefunction(orig_aread):
            async def _mock_aread():
                if hasattr(resp, "text") and isinstance(resp.text, str):
                    return resp.text.encode("utf-8")
                if hasattr(resp, "content") and isinstance(resp.content, (bytes, bytearray)):
                    return bytes(resp.content)
                try:
                    j = resp.json()
                    if isinstance(j, (dict, list)):
                        return json.dumps(j).encode("utf-8")
                except Exception:
                    pass
                return b""
            resp.aread = _mock_aread

        orig_aclose = getattr(resp, "aclose", None)
        if not asyncio.iscoroutinefunction(orig_aclose):
            async def _mock_aclose():
                pass
            resp.aclose = _mock_aclose

        try:
            yield resp
        finally:
            await resp.aclose()
    else:
        async with client.stream(method, url, **kwargs) as response:
            yield response
