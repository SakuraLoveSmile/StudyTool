"""Persistence and application service for the daily-learning flow.

The service intentionally contains no LLM calls and no FastAPI concerns.  The
learning router can pass an existing sqlite connection, but every write also
works when called directly.  Dates at the HTTP boundary are explicit whenever
possible; when omitted, the service uses the user's configured
``Asia/Shanghai`` calendar day.

Schema ownership lives in ``database.py``/the schema migration.  This module
only reads and writes the six learning tables (and the recall-card tables) and
therefore stays usable while the schema migration is applied independently.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from app.database import get_db_connection
from app.services.learning_scheduler import (
    NEW_CARD_ROUNDS,
    STATUS_LEARNING,
    STATUS_NEW,
    STATUS_REVIEW,
    complete_new_card,
    transition_state,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
SETTINGS_ID = "singleton"
LAYERS = ("outline", "expanded")
RATINGS = ("forgotten", "fuzzy", "mastered")
DAY_STATUSES = ("active", "paused", "completed", "archived")


class LearningServiceError(Exception):
    """An expected domain error suitable for direct router translation.

    ``code`` is stable for clients/tests, ``message`` is user-facing, and
    ``status_code`` is the HTTP status a conventional router should use.
    """

    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code = str(code)
        self.message = str(message)
        self.status_code = int(status_code)
        # FastAPI integrations often use ``detail`` directly.
        self.detail = {"code": self.code, "message": self.message}
        super().__init__(self.message)


# ---------------------------------------------------------------------------
# Generic boundary helpers
# ---------------------------------------------------------------------------


def _mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    # Pydantic v2, dataclasses and simple request objects.
    if hasattr(value, "model_dump"):
        try:
            return dict(value.model_dump(exclude_unset=True))
        except TypeError:
            return dict(value.model_dump())
    if hasattr(value, "dict"):
        try:
            return dict(value.dict(exclude_unset=True))
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        return {
            k: v for k, v in vars(value).items() if not k.startswith("_")
        }
    raise LearningServiceError("INVALID_REQUEST", "请求参数格式不正确", 422)


def _is_connection(value: Any) -> bool:
    return isinstance(value, sqlite3.Connection) or (
        value is not None
        and hasattr(value, "execute")
        and hasattr(value, "commit")
        and hasattr(value, "rollback")
    )


def _as_utc_timestamp(value: Any) -> Any:
    """Return a timezone-aware UTC ISO-8601 string for stored timestamps.

    SQLite's ``CURRENT_TIMESTAMP`` is already UTC but is stored without a
    timezone designator.  Exposing it verbatim would give clients a naive
    string, so every ``*_at`` column is normalised to an explicit ``+00:00``
    offset on the way out.  Date-only columns (``study_date``/``due_date``) are
    deliberately left untouched.
    """
    if not isinstance(value, str) or not value:
        return value
    text = value.strip()
    if text.endswith("Z") or "+" in text[10:] or text[10:].count("-") > 0:
        return text
    candidate = text.replace(" ", "T")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _normalise_timestamps(row: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not row:
        return row
    for key, value in list(row.items()):
        if key.endswith("_at"):
            row[key] = _as_utc_timestamp(value)
    return row


def _row_dict(row: Any, cursor: Optional[sqlite3.Cursor] = None) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return _normalise_timestamps({key: row[key] for key in row.keys()})
    if isinstance(row, Mapping):
        return _normalise_timestamps(dict(row))
    if cursor is not None and cursor.description:
        return _normalise_timestamps({item[0]: row[i] for i, item in enumerate(cursor.description)})
    try:
        return _normalise_timestamps(dict(row))
    except Exception:
        return None


def _rows(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    cur = conn.execute(sql, tuple(params))
    return [item for row in cur.fetchall() if (item := _row_dict(row, cur)) is not None]


def _one(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> Optional[dict[str, Any]]:
    cur = conn.execute(sql, tuple(params))
    return _row_dict(cur.fetchone(), cur)


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, sqlite3.Row):
        return dict(value)
    raise TypeError(f"not JSON serializable: {type(value)!r}")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default)


def _loads(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _first(data: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in data and data[name] is not None:
            return data[name]
    return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y", "是"}
    return bool(value)


def _as_int(value: Any, name: str, *, minimum: Optional[int] = None, maximum: Optional[int] = None) -> int:
    if isinstance(value, bool):
        raise LearningServiceError("INVALID_REQUEST", f"{name} 必须是整数", 422)
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise LearningServiceError("INVALID_REQUEST", f"{name} 必须是整数", 422) from exc
    if minimum is not None and result < minimum:
        raise LearningServiceError("INVALID_REQUEST", f"{name} 不能小于 {minimum}", 422)
    if maximum is not None and result > maximum:
        raise LearningServiceError("INVALID_REQUEST", f"{name} 不能大于 {maximum}", 422)
    return result


def _normalise_ids(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        # Accept a JSON array from a form/query boundary, while retaining a
        # single id as a useful convenience.
        stripped = value.strip()
        if not stripped:
            return []
        parsed = _loads(stripped, None)
        value = parsed if isinstance(parsed, list) else [stripped]
    if not isinstance(value, (list, tuple, set)):
        raise LearningServiceError("INVALID_REQUEST", f"{name} 必须是 ID 数组", 422)
    result: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _new_id() -> str:
    return str(uuid.uuid4())


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalise_title(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().casefold().split())


def _parse_date(value: Any, name: str = "study_date") -> date:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(SHANGHAI).date()
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError as exc:
            raise LearningServiceError("INVALID_DATE", f"{name} 必须是 YYYY-MM-DD", 422) from exc
    raise LearningServiceError("INVALID_DATE", f"{name} 必须是 YYYY-MM-DD", 422)


def resolve_study_date(
    study_date: Any = None,
    *,
    now: Any = None,
    clock: Optional[Callable[[], Any]] = None,
) -> date:
    """Resolve a study date deterministically.

    An explicitly supplied date never consults the clock.  This is the hook
    used by lifecycle tests and migration/replay code.  Production calls omit
    it and use the Asia/Shanghai calendar day.
    """

    if study_date is not None:
        return _parse_date(study_date)
    candidate = now
    if candidate is None and clock is not None:
        candidate = clock() if callable(clock) else clock
    if candidate is None:
        return datetime.now(SHANGHAI).date()
    if callable(candidate):
        candidate = candidate()
    if isinstance(candidate, datetime):
        if candidate.tzinfo is None:
            return candidate.date()
        return candidate.astimezone(SHANGHAI).date()
    return _parse_date(candidate, "now")


def _current_date_for_check(now: Any = None, clock: Optional[Callable[[], Any]] = None) -> date:
    # Unlike resolve_study_date, this always represents the wall clock when no
    # test hook was supplied.
    return resolve_study_date(None, now=now, clock=clock)


@contextmanager
def _write_tx(conn: sqlite3.Connection):
    """A short BEGIN IMMEDIATE transaction, nesting safely in caller txs."""

    savepoint: Optional[str] = None
    began = False
    try:
        if getattr(conn, "in_transaction", False):
            savepoint = f"learning_{uuid.uuid4().hex}"
            conn.execute(f"SAVEPOINT {savepoint}")
        else:
            conn.execute("BEGIN IMMEDIATE")
            began = True
        yield conn
        if savepoint:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        elif began:
            conn.commit()
    except Exception:
        try:
            if savepoint:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            elif began:
                conn.rollback()
        finally:
            raise


@contextmanager
def _connection(connection: Optional[sqlite3.Connection]):
    if connection is not None:
        yield connection, False
        return
    conn = get_db_connection()
    try:
        yield conn, True
    finally:
        conn.close()


def _coerce_connection_date(
    args: tuple[Any, ...],
    conn: Optional[sqlite3.Connection],
    study_date: Any,
) -> tuple[Optional[sqlite3.Connection], Any]:
    """Allow both ``method(conn, date)`` and named/date-first boundaries."""

    values = list(args)
    if values and _is_connection(values[0]):
        conn = values.pop(0)
    if values and study_date is None:
        study_date = values.pop(0)
    if values:
        raise TypeError("too many positional arguments")
    return conn, study_date


def _coerce_data_connection(
    args: tuple[Any, ...],
    data: Any,
    conn: Optional[sqlite3.Connection],
) -> tuple[Any, Optional[sqlite3.Connection]]:
    values = list(args)
    if values and _is_connection(values[0]):
        conn = values.pop(0)
    if values and data is None:
        data = values.pop(0)
    if values:
        raise TypeError("too many positional arguments")
    return data, conn


# ---------------------------------------------------------------------------
# Learning service
# ---------------------------------------------------------------------------


class LearningService:
    """Daily learning application service.

    ``clock`` is optional and intended for deterministic tests.  It should
    return a date/datetime; it is never persisted and there is no production
    date override endpoint.
    """

    def __init__(self, *, clock: Optional[Callable[[], Any]] = None):
        self.clock = clock

    # -- date/error/version helpers -------------------------------------

    @classmethod
    def _clock_date(cls, now: Any = None, clock: Optional[Callable[[], Any]] = None) -> date:
        return _current_date_for_check(now, clock)

    def _date(self, study_date: Any = None, *, now: Any = None) -> date:
        return resolve_study_date(study_date, now=now, clock=self.clock)

    @classmethod
    def _check_cross_day(
        cls,
        requested: date,
        *,
        study_date_supplied: bool,
        now: Any = None,
        clock: Optional[Callable[[], Any]] = None,
        current_date: Any = None,
        enforce_today: Optional[bool] = None,
    ) -> None:
        """Raise DAY_CHANGED when an action is known to cross a local day.

        Explicit dates are deliberately accepted for deterministic replay.  A
        router can pass ``enforce_today=True`` (or ``current_date``/``now``)
        for live day-bound actions, which gives the intended cross-day error.
        """

        if current_date is not None:
            wall = _parse_date(current_date, "current_date")
        elif now is not None or clock is not None:
            wall = _current_date_for_check(now, clock)
        elif enforce_today is True:
            wall = _current_date_for_check(None, clock)
        else:
            return
        if requested != wall:
            raise LearningServiceError(
                "DAY_CHANGED",
                f"学习日已切换为 {wall.isoformat()}，请刷新今日学习内容",
                409,
            )

    @staticmethod
    def _check_day_version(day: Mapping[str, Any], expected: Any = None) -> None:
        if expected is None:
            return
        expected_int = _as_int(expected, "day_version", minimum=1)
        actual = int(day.get("state_version") or 1)
        if expected_int != actual:
            raise LearningServiceError(
                "DAY_VERSION_CONFLICT",
                "学习日版本已变化，请刷新后重试",
                409,
            )

    @staticmethod
    def _check_content_version(unit: Mapping[str, Any], expected: Any = None) -> None:
        if expected is None:
            return
        expected_int = _as_int(expected, "content_revision_no", minimum=1)
        actual = int(unit.get("content_revision_no") or 0)
        if expected_int != actual:
            raise LearningServiceError(
                "CONTENT_VERSION_CONFLICT",
                "学习卡内容已更新，请刷新后重新开始该题",
                409,
            )

    @staticmethod
    def _request_values(data: Mapping[str, Any], **aliases: Sequence[str]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for target, names in aliases.items():
            result[target] = _first(data, *names)
        return result

    # -- settings --------------------------------------------------------

    @classmethod
    def _settings_dict(cls, row: Optional[Mapping[str, Any]]) -> dict[str, Any]:
        """Return one stable settings representation across v6 migrations.

        The migration keeps both the plan names (``daily_new_limit``) and the
        UI names (``new_cards_per_day``).  Nullable limits remain nullable: a
        missing override means use the corresponding configured daily default,
        rather than inventing a hidden service default.
        """
        if not row:
            return {
                "id": SETTINGS_ID,
                # ``None`` is intentional: an absent setting means no explicit
                # limit, not an undocumented service default.
                "daily_new_limit": None,
                "daily_review_limit": None,
                "new_cards_per_day": None,
                "review_cards_per_day": None,
                "reveal_after_seconds": 0,
                "session_minutes": 20,
                "timezone": "Asia/Shanghai",
                "selected_chapter_ids": [],
                "selected_knowledge_point_ids": [],
                "selected_chapter_ids_json": "[]",
                "selected_knowledge_point_ids_json": "[]",
                "include_unassigned": True,
                "auto_advance": False,
                "show_hints": True,
                "version": 1,
                "updated_at": None,
            }
        chapters = _loads(row.get("selected_chapter_ids_json"), [])
        kps = _loads(row.get("selected_knowledge_point_ids_json"), [])
        if not isinstance(chapters, list):
            chapters = []
        if not isinstance(kps, list):
            kps = []
        result = dict(row)
        result["id"] = result.get("id") or SETTINGS_ID
        for key in ("daily_new_limit", "daily_review_limit", "new_cards_per_day", "review_cards_per_day"):
            result[key] = None if result.get(key) is None else int(result[key])
        # Keep nullable limits nullable.  ``new_cards_per_day`` and
        # ``review_cards_per_day`` are compatibility aliases, not defaults.
        if result.get("daily_new_limit") is None and result.get("new_cards_per_day") is not None:
            result["daily_new_limit"] = int(result["new_cards_per_day"])
        if result.get("daily_review_limit") is None and result.get("review_cards_per_day") is not None:
            result["daily_review_limit"] = int(result["review_cards_per_day"])
        if result.get("new_cards_per_day") is None:
            result["new_cards_per_day"] = result.get("daily_new_limit")
        if result.get("review_cards_per_day") is None:
            result["review_cards_per_day"] = result.get("daily_review_limit")
        result["reveal_after_seconds"] = int(result.get("reveal_after_seconds") or 0)
        result["session_minutes"] = int(result.get("session_minutes") or 20)
        result["timezone"] = result.get("timezone") or "Asia/Shanghai"
        result["selected_chapter_ids"] = [str(x) for x in chapters]
        result["selected_knowledge_point_ids"] = [str(x) for x in kps]
        result["selected_chapter_ids_json"] = _json(result["selected_chapter_ids"])
        result["selected_knowledge_point_ids_json"] = _json(result["selected_knowledge_point_ids"])
        result["include_unassigned"] = _as_bool(result.get("include_unassigned"), True)
        result["auto_advance"] = _as_bool(result.get("auto_advance"), False)
        result["show_hints"] = _as_bool(result.get("show_hints"), True)
        result["version"] = int(result.get("version") or 1)
        return result

    @classmethod
    def get_settings(cls, *args: Any, conn: Optional[sqlite3.Connection] = None) -> dict[str, Any]:
        conn, _ = _coerce_connection_date(args, conn, None)
        with _connection(conn) as (db, _owned):
            row = _one(db, "SELECT * FROM learning_settings WHERE id = ?", (SETTINGS_ID,))
            # A migration may have used a different singleton key.  There must
            # only be one settings row, so use it as a compatibility fallback.
            if row is None:
                rows = _rows(db, "SELECT * FROM learning_settings ORDER BY updated_at DESC, id LIMIT 1")
                row = rows[0] if rows else None
            return cls._settings_dict(row)

    @classmethod
    def update_settings(
        cls,
        *args: Any,
        data: Any = None,
        conn: Optional[sqlite3.Connection] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        data, conn = _coerce_data_connection(args, data, conn)
        values = _mapping(data)
        values.update(kwargs)
        expected_settings_version = _first(values, "expected_version", "version")
        # Support a request object whose fields use the JSON column names.
        daily = _first(values, "daily_new_limit", "new_limit", "new_cards_per_day")
        if daily is not None:
            daily = _as_int(daily, "daily_new_limit", minimum=1, maximum=100)
        review_limit = _first(values, "daily_review_limit", "review_cards_per_day")
        if review_limit is not None:
            review_limit = _as_int(review_limit, "daily_review_limit", minimum=1, maximum=100)
        timezone_name = _first(values, "timezone", default="Asia/Shanghai")
        include_unassigned = _as_bool(_first(values, "include_unassigned", default=True), True)
        auto_advance = _as_bool(_first(values, "auto_advance", default=False), False)
        show_hints = _as_bool(_first(values, "show_hints", default=True), True)
        reveal_after_seconds = _first(values, "reveal_after_seconds")
        if reveal_after_seconds is not None:
            reveal_after_seconds = _as_int(reveal_after_seconds, "reveal_after_seconds", minimum=0, maximum=86400)
        else:
            reveal_after_seconds = 0
        if timezone_name != "Asia/Shanghai":
            raise LearningServiceError("INVALID_TIMEZONE", "当前版本仅支持 Asia/Shanghai", 422)
        chapter_ids = _normalise_ids(
            _first(values, "selected_chapter_ids", "chapter_ids", "selected_chapters"),
            "selected_chapter_ids",
        )
        kp_ids = _normalise_ids(
            _first(values, "selected_knowledge_point_ids", "knowledge_point_ids", "selected_knowledge_points"),
            "selected_knowledge_point_ids",
        )
        with _connection(conn) as (db, _owned):
            with _write_tx(db):
                existing = _one(db, "SELECT * FROM learning_settings WHERE id = ?", (SETTINGS_ID,))
                if existing is None:
                    # Preserve nullable new-limit semantics: no hidden default.
                    db.execute(
                        """
                        INSERT INTO learning_settings
                          (id, daily_new_limit, daily_review_limit,
                           new_cards_per_day, review_cards_per_day, timezone,
                           selected_chapter_ids_json, selected_knowledge_point_ids_json,
                           include_unassigned, auto_advance, show_hints, reveal_after_seconds,
                           version, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
                        """,
                        (SETTINGS_ID, daily, review_limit, daily, review_limit,
                         timezone_name, _json(chapter_ids), _json(kp_ids),
                          1 if include_unassigned else 0, 1 if auto_advance else 0,
                          1 if show_hints else 0, reveal_after_seconds),
                    )
                else:
                    if expected_settings_version is not None and int(existing.get("version") or 1) != _as_int(expected_settings_version, "expected_version", minimum=1):
                        raise LearningServiceError("VERSION_CONFLICT", "学习设置已变化，请刷新后重试", 409)
                    if "daily_new_limit" not in values and "new_limit" not in values and "new_cards_per_day" not in values:
                        daily = existing.get("daily_new_limit")
                    if "daily_review_limit" not in values and "review_cards_per_day" not in values:
                        review_limit = existing.get("daily_review_limit")
                    if not (set(values) & {"selected_chapter_ids", "chapter_ids", "selected_chapters"}):
                        chapter_ids = _loads(existing.get("selected_chapter_ids_json"), [])
                        chapter_ids = _normalise_ids(chapter_ids, "selected_chapter_ids")
                    if not (set(values) & {"selected_knowledge_point_ids", "knowledge_point_ids", "selected_knowledge_points"}):
                        kp_ids = _loads(existing.get("selected_knowledge_point_ids_json"), [])
                        kp_ids = _normalise_ids(kp_ids, "selected_knowledge_point_ids")
                    db.execute(
                        """
                        UPDATE learning_settings
                        SET daily_new_limit = ?, daily_review_limit = ?,
                            new_cards_per_day = ?, review_cards_per_day = ?,
                            timezone = ?, selected_chapter_ids_json = ?,
                            selected_knowledge_point_ids_json = ?, include_unassigned = ?, auto_advance = ?, show_hints = ?, reveal_after_seconds = ?,
                            version = COALESCE(version, 1) + 1,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (daily, review_limit, daily, review_limit, timezone_name,
                         _json(chapter_ids), _json(kp_ids), 1 if include_unassigned else 0,
                          1 if auto_advance else 0, 1 if show_hints else 0,
                          reveal_after_seconds, existing.get("id") or SETTINGS_ID),
                    )
            return cls.get_settings(conn=db)

    @classmethod
    def save_scope(
        cls,
        *args: Any,
        chapter_ids: Any = None,
        knowledge_point_ids: Any = None,
        data: Any = None,
        conn: Optional[sqlite3.Connection] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        data, conn = _coerce_data_connection(args, data, conn)
        values = _mapping(data)
        values.update(kwargs)
        if chapter_ids is not None:
            values["selected_chapter_ids"] = chapter_ids
        if knowledge_point_ids is not None:
            values["selected_knowledge_point_ids"] = knowledge_point_ids
        # Scope save is intentionally a partial settings update.
        return cls.update_settings(data=values, conn=conn)

    # -- chapters --------------------------------------------------------

    @classmethod
    def list_chapters(
        cls,
        *args: Any,
        conn: Optional[sqlite3.Connection] = None,
    ) -> list[dict[str, Any]]:
        conn, _ = _coerce_connection_date(args, conn, None)
        with _connection(conn) as (db, _owned):
            rows = _rows(
                db,
                """
                SELECT c.*,
                       (SELECT COUNT(*) FROM recall_cards rc
                        WHERE rc.chapter_id = c.id AND rc.is_archived = 0) AS cards_count,
                       (SELECT COUNT(*) FROM recall_cards rc
                        JOIN recall_card_revisions rr ON rr.card_id = rc.id
                                                     AND rr.revision_no = rc.current_revision_no
                        WHERE rc.chapter_id = c.id AND rc.is_archived = 0) AS ready_cards_count
                FROM recall_chapters c
                ORDER BY c.sort_order ASC, c.title COLLATE NOCASE ASC, c.id ASC
                """,
            )
            for row in rows:
                row["expanded_enabled"] = bool(row.get("expanded_enabled"))
                row["cards_count"] = int(row.get("cards_count") or 0)
                row["ready_cards_count"] = int(row.get("ready_cards_count") or 0)
            return rows

    @classmethod
    def create_chapter(
        cls,
        *args: Any,
        data: Any = None,
        conn: Optional[sqlite3.Connection] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        data, conn = _coerce_data_connection(args, data, conn)
        values = _mapping(data)
        values.update(kwargs)
        title = str(_first(values, "title", "name", default="")).strip()
        normalized = _normalise_title(title)
        if not title or not normalized:
            raise LearningServiceError("INVALID_REQUEST", "章节标题不能为空", 422)
        chapter_id = str(_first(values, "id", "chapter_id", default=_new_id()))
        source_doc_id = _first(values, "source_doc_id", "doc_id")
        source_name = _first(values, "source_doc_name_snapshot", "source_doc_name", "doc_name")
        sort_order = _as_int(_first(values, "sort_order", default=0), "sort_order")
        expanded = 1 if _as_bool(_first(values, "expanded_enabled", default=False)) else 0
        with _connection(conn) as (db, _owned):
            with _write_tx(db):
                if source_doc_id and not source_name:
                    doc = _one(db, "SELECT filename FROM documents WHERE id = ?", (source_doc_id,))
                    source_name = doc.get("filename") if doc else None
                duplicate = _one(
                    db,
                    "SELECT id FROM recall_chapters WHERE normalized_title = ?",
                    (normalized,),
                )
                if duplicate:
                    raise LearningServiceError("CHAPTER_EXISTS", "同名章节已存在", 409)
                try:
                    db.execute(
                        """
                        INSERT INTO recall_chapters
                          (id, source_doc_id, source_doc_name_snapshot, title, normalized_title,
                           sort_order, expanded_enabled, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                        """,
                        (chapter_id, source_doc_id, source_name, title, normalized, sort_order, expanded),
                    )
                except sqlite3.IntegrityError as exc:
                    raise LearningServiceError("CHAPTER_EXISTS", "章节已存在", 409) from exc
            result = _one(db, "SELECT * FROM recall_chapters WHERE id = ?", (chapter_id,))
            return result or {"id": chapter_id, "title": title, "normalized_title": normalized}

    @classmethod
    def update_chapter(
        cls,
        chapter_id: str,
        *args: Any,
        data: Any = None,
        conn: Optional[sqlite3.Connection] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        data, conn = _coerce_data_connection(args, data, conn)
        values = _mapping(data)
        values.update(kwargs)
        with _connection(conn) as (db, _owned):
            with _write_tx(db):
                current = _one(db, "SELECT * FROM recall_chapters WHERE id = ?", (chapter_id,))
                if not current:
                    raise LearningServiceError("CHAPTER_NOT_FOUND", "章节不存在", 404)
                updates: list[str] = []
                params: list[Any] = []
                if "title" in values or "name" in values:
                    title = str(_first(values, "title", "name")).strip()
                    normalized = _normalise_title(title)
                    if not normalized:
                        raise LearningServiceError("INVALID_REQUEST", "章节标题不能为空", 422)
                    duplicate = _one(
                        db,
                        "SELECT id FROM recall_chapters WHERE normalized_title = ? AND id <> ?",
                        (normalized, chapter_id),
                    )
                    if duplicate:
                        raise LearningServiceError("CHAPTER_EXISTS", "同名章节已存在", 409)
                    updates.extend(["title = ?", "normalized_title = ?"])
                    params.extend([title, normalized])
                for key, column in (
                    ("source_doc_id", "source_doc_id"),
                    ("source_doc_name_snapshot", "source_doc_name_snapshot"),
                ):
                    if key in values:
                        updates.append(f"{column} = ?")
                        params.append(values[key])
                if "sort_order" in values:
                    updates.append("sort_order = ?")
                    params.append(_as_int(values["sort_order"], "sort_order"))
                if "expanded_enabled" in values:
                    updates.append("expanded_enabled = ?")
                    params.append(1 if _as_bool(values["expanded_enabled"]) else 0)
                if updates:
                    params.append(chapter_id)
                    db.execute(
                        f"UPDATE recall_chapters SET {', '.join(updates)}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        tuple(params),
                    )
            result = _one(db, "SELECT * FROM recall_chapters WHERE id = ?", (chapter_id,))
            if result:
                result["expanded_enabled"] = bool(result.get("expanded_enabled"))
            return result or {}

    # -- recall-card/queue internals ------------------------------------

    @classmethod
    def _card_candidates(cls, db: sqlite3.Connection, settings: Mapping[str, Any], override: bool) -> list[dict[str, Any]]:
        rows = _rows(
            db,
            """
            SELECT rc.id AS card_id, rc.chapter_id, rc.knowledge_point_id,
                   rc.current_revision_no, rc.is_paused, rc.is_archived,
                   rc.sort_order AS card_sort_order, rc.lock_version,
                   ch.title AS chapter_title, ch.sort_order AS chapter_sort_order,
                   ch.expanded_enabled,
                   rr.id AS revision_id,
                   COALESCE(NULLIF(rr.revision_no, 0), rr.revision_number) AS revision_no,
                   COALESCE(NULLIF(rr.review_status, ''), rr.status) AS review_status,
                   COALESCE(rr.question, rr.prompt, rr.front_content) AS question,
                   COALESCE(NULLIF(rr.card_type, ''), rc.card_type) AS card_type,
                   rr.outline_points_json, rr.explanation, rr.expanded_answer,
                   COALESCE(rr.citations_json, rr.source_citations_json) AS citations_json,
                   rr.source_status, rr.unit_key
            FROM recall_cards rc
            JOIN recall_chapters ch ON ch.id = rc.chapter_id
            JOIN recall_card_revisions rr
              ON rr.card_id = rc.id
             AND (rr.id = rc.published_revision_id OR
                  (rc.published_revision_id IS NULL AND
                   COALESCE(NULLIF(rr.revision_no, 0), rr.revision_number) = rc.current_revision_no))
            WHERE rc.is_archived = 0
              AND COALESCE(rc.status, '') NOT IN ('archived', 'rejected', 'draft')
              AND COALESCE(rc.publication_status, rc.status, '') IN ('published', 'approved')
               AND COALESCE(rc.is_confirmed, 0) = 1
               AND COALESCE(rc.is_ai_generated, 0) = 0
            ORDER BY ch.sort_order ASC, rc.sort_order ASC, rc.id ASC
            """,
        )
        selected_chapters = set(_normalise_ids(settings.get("selected_chapter_ids"), "selected_chapter_ids"))
        selected_kps = set(_normalise_ids(settings.get("selected_knowledge_point_ids"), "selected_knowledge_point_ids"))
        result: list[dict[str, Any]] = []
        for row in rows:
            # Paused/invalid cards are excluded from the normal queue.  An
            # explicit override is allowed to inspect paused cards, but never
            # archived cards (archive is a durable content decision).
            if _as_bool(row.get("is_paused")) and not override:
                continue
            if selected_chapters and str(row.get("chapter_id")) not in selected_chapters:
                continue
            if selected_kps and str(row.get("knowledge_point_id")) not in selected_kps:
                continue
            review_status = str(row.get("review_status") or "").strip().lower()
            source_status = str(row.get("source_status") or "").strip().lower()
            if review_status in {"rejected", "invalid", "archived", "deleted", "draft"}:
                continue
            if source_status in {"invalid", "rejected", "deleted"}:
                continue
            # A card revision must have at least a question or answer payload;
            # malformed revisions are not actionable and are left visible to
            # content management rather than causing a queue crash.
            if not any(row.get(key) for key in ("question", "outline_points_json", "explanation", "expanded_answer")):
                continue
            result.append(row)
        return result

    @classmethod
    def _progress(cls, db: sqlite3.Connection, card_id: str, layer: str) -> Optional[dict[str, Any]]:
        return _one(
            db,
            "SELECT * FROM learning_progress WHERE card_id = ? AND layer = ?",
            (card_id, layer),
        )

    @classmethod
    def _ensure_progress(
        cls,
        db: sqlite3.Connection,
        card_id: str,
        layer: str,
        content_revision_no: int,
        study_date: date,
        *,
        chapter_id: Optional[str] = None,
        card_revision_id: Optional[str] = None,
    ) -> dict[str, Any]:
        progress = cls._progress(db, card_id, layer)
        if chapter_id is None or card_revision_id is None:
            card = _one(db, "SELECT chapter_id, current_revision_id FROM recall_cards WHERE id = ?", (card_id,))
            if card:
                chapter_id = chapter_id or card.get("chapter_id")
                card_revision_id = card_revision_id or card.get("current_revision_id")
        if progress is None:
            progress_id = _new_id()
            try:
                db.execute(
                    """
                    INSERT INTO learning_progress
                      (id, card_id, chapter_id, card_revision_id, layer,
                       content_revision_no, state, status, interval_index, new_round,
                       due_date, first_study_date, state_version, version, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'new', 'new', 0, 0, ?, NULL, 1, 1, CURRENT_TIMESTAMP)
                    """,
                    (progress_id, card_id, chapter_id, card_revision_id, layer,
                     content_revision_no, study_date.isoformat()),
                )
            except sqlite3.IntegrityError:
                # A concurrent writer may have won the unique(card,layer) race.
                progress = cls._progress(db, card_id, layer)
            else:
                progress = cls._progress(db, card_id, layer)
        elif int(progress.get("content_revision_no") or 0) != int(content_revision_no):
            # A normal publish keeps the existing schedule. Only the explicit
            # reset_progress operation starts the card over.
            db.execute(
                """
                UPDATE learning_progress
                SET chapter_id = COALESCE(?, chapter_id), card_revision_id = COALESCE(?, card_revision_id),
                    content_revision_no = ?, state_version = COALESCE(state_version, 1) + 1,
                    version = COALESCE(version, 1) + 1, updated_at = CURRENT_TIMESTAMP
                WHERE card_id = ? AND layer = ?
                """,
                (chapter_id, card_revision_id, content_revision_no, card_id, layer),
            )
            progress = cls._progress(db, card_id, layer)
        if progress is None:  # pragma: no cover - defensive against malformed schema
            raise LearningServiceError("SCHEMA_ERROR", "学习进度无法创建", 500)
        return progress

    @classmethod
    def _unit_type(cls, progress: Optional[Mapping[str, Any]], study_date: date) -> Optional[str]:
        if progress is None:
            return "new"
        state = str(progress.get("state") or STATUS_NEW)
        due_raw = progress.get("due_date")
        due = _parse_date(due_raw, "due_date") if due_raw else study_date
        if due > study_date:
            return None
        if state in {STATUS_NEW, STATUS_LEARNING}:
            # Carryover means "started earlier but the same-day rounds were not
            # finished".  A card that was only queued and never attempted stays
            # new material and must still obey the explicit daily new limit.
            started = int(progress.get("repetitions") or 0) > 0 or bool(progress.get("last_studied_at"))
            if started and due < study_date:
                return "carryover"
            return "new"
        return "review"

    @classmethod
    def _available_layers(cls, card: Mapping[str, Any]) -> list[str]:
        result = ["outline"]
        if _as_bool(card.get("expanded_enabled")) and any(
            card.get(key) for key in ("expanded_answer", "explanation")
        ):
            result.append("expanded")
        return result

    @classmethod
    def _unit_existing(cls, db: sqlite3.Connection, study_date: date, card_id: str, layer: str) -> Optional[dict[str, Any]]:
        return _one(
            db,
            "SELECT * FROM learning_day_units WHERE study_date = ? AND card_id = ? AND layer = ?",
            (study_date.isoformat(), card_id, layer),
        )

    @classmethod
    def _mark_stale_unit(cls, db: sqlite3.Connection, unit: Mapping[str, Any], reason: str) -> None:
        db.execute(
            "UPDATE learning_day_units SET invalid_reason = ? WHERE id = ?",
            (reason, unit.get("id")),
        )
        # Preserve an audit trail but make stale attempts impossible to submit.
        db.execute(
            "UPDATE learning_attempts SET status = 'invalid' WHERE day_unit_id = ? AND status IN ('pending', 'revealed')",
            (unit.get("id"),),
        )

    @classmethod
    def _insert_attempt(
        cls,
        db: sqlite3.Connection,
        unit: Mapping[str, Any],
        round_no: int,
        *,
        attempt_kind: str = "primary",
        attempt_index: int = 0,
        display_order: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        if round_no < 1 or round_no > NEW_CARD_ROUNDS:
            return None
        existing = _one(
            db,
            """
            SELECT * FROM learning_attempts
            WHERE day_unit_id = ? AND round = ? AND attempt_index = ?
            """,
            (unit["id"], round_no, attempt_index),
        )
        if existing:
            return existing
        attempt_id = _new_id()
        if display_order is None:
            display_order = int(unit.get("display_order") or 0) * 10 + attempt_index
        try:
            db.execute(
                """
                INSERT INTO learning_attempts
                  (id, day_unit_id, learning_day_id, study_date, card_id, chapter_id,
                   layer, card_revision_id, content_revision_no, round, attempt_kind,
                   attempt_index, display_order, revealed_at, rating, submitted_at,
                   completed_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, 'pending')
                """,
                (
                    attempt_id,
                    unit["id"],
                    unit.get("learning_day_id"),
                    unit.get("study_date"),
                    unit["card_id"],
                    unit["chapter_id"],
                    unit.get("layer") or "outline",
                    unit.get("card_revision_id"),
                    int(unit.get("content_revision_no") or 0),
                    round_no,
                    attempt_kind,
                    attempt_index,
                    display_order,
                ),
            )
        except sqlite3.IntegrityError:
            return _one(
                db,
                """
                SELECT * FROM learning_attempts
                WHERE day_unit_id = ? AND round = ? AND attempt_index = ?
                """,
                (unit["id"], round_no, attempt_index),
            )
        return _one(db, "SELECT * FROM learning_attempts WHERE id = ?", (attempt_id,))

    @classmethod
    def _build_units(
        cls,
        db: sqlite3.Connection,
        day: Mapping[str, Any],
        study_date: date,
        *,
        force: bool = False,
    ) -> int:
        settings = cls._settings_dict(
            _one(db, "SELECT * FROM learning_settings WHERE id = ?", (SETTINGS_ID,))
            or ((_rows(db, "SELECT * FROM learning_settings ORDER BY updated_at DESC, id LIMIT 1") or [None])[0])
        )
        override = force or _as_bool(day.get("override_enabled"))
        cards = cls._card_candidates(db, settings, override)

        # Resolve progress and unit type first.  New limits apply to cards, not
        # layers; this prevents expanded mode from doubling the user's limit.
        prepared: list[tuple[dict[str, Any], str, dict[str, Any], str]] = []
        new_card_ids: list[str] = []
        for card in cards:
            revision = int(card.get("revision_no") or card.get("current_revision_no") or 0)
            if revision < 1:
                continue
            for layer in cls._available_layers(card):
                progress = cls._progress(db, str(card["card_id"]), layer)
                unit_type = cls._unit_type(progress, study_date)
                if unit_type is None:
                    continue
                # Only selected new cards need a progress row.  This avoids
                # making every untouched card look studied before it is queued.
                if progress is None and unit_type == "new":
                    progress = cls._ensure_progress(
                        db, str(card["card_id"]), layer, revision, study_date,
                        chapter_id=str(card.get("chapter_id") or ""),
                        card_revision_id=str(card.get("revision_id") or "") or None,
                    )
                elif progress is not None and int(progress.get("content_revision_no") or 0) != revision:
                    progress = cls._ensure_progress(
                        db, str(card["card_id"]), layer, revision, study_date,
                        chapter_id=str(card.get("chapter_id") or ""),
                        card_revision_id=str(card.get("revision_id") or "") or None,
                    )
                if progress is None:
                    continue
                # Re-evaluate after lazy progress creation.
                unit_type = cls._unit_type(progress, study_date)
                if unit_type is None:
                    continue
                if unit_type == "new" and str(card["card_id"]) not in new_card_ids:
                    new_card_ids.append(str(card["card_id"]))
                prepared.append((card, layer, progress, unit_type))

        limit = settings.get("daily_new_limit")
        due_items = [item for item in prepared if item[3] in {"carryover", "review"}]
        if due_items and not override:
            # Due reviews and unfinished carryovers take precedence over new
            # cards.  A user can explicitly enable the one-day override to opt
            # into new material without silently changing the normal queue.
            prepared = [item for item in prepared if item[3] != "new"]
        elif limit is None and not override:
            # ``daily_new_limit`` has no hidden default: until the user states a
            # number between 1 and 100 the queue only carries due reviews and
            # carryover, never an implicit "learn everything".
            prepared = [item for item in prepared if item[3] != "new"]
        elif not override:
            allowed = set(new_card_ids[: int(limit)])
            prepared = [
                item for item in prepared
                if item[3] != "new" or str(item[0]["card_id"]) in allowed
            ]

        # Apply the review cap only to ordinary due reviews. Carryover is never
        # hidden by a daily cap because it is unfinished prior-day work.
        review_limit = settings.get("daily_review_limit")
        if review_limit is not None and not override:
            review_seen = 0
            limited: list[tuple[dict[str, Any], str, dict[str, Any], str]] = []
            for item in prepared:
                if item[3] == "review":
                    card_id = str(item[0]["card_id"])
                    if card_id not in {str(x[0]["card_id"]) for x in limited if x[3] == "review"}:
                        if review_seen >= int(review_limit):
                            continue
                        review_seen += 1
                limited.append(item)
            prepared = limited

        # The queue order is deterministic: carryover first, then oldest due
        # review, then new.  Chapter/card/layer order is the tie breaker.
        def sort_key(item: tuple[dict[str, Any], str, dict[str, Any], str]):
            card, layer, progress, unit_type = item
            due = progress.get("due_date") or study_date.isoformat()
            priority = {"carryover": 0, "review": 1, "new": 2}.get(unit_type, 3)
            return (
                priority,
                due if priority < 2 else "",
                int(card.get("chapter_sort_order") or 0),
                int(card.get("card_sort_order") or 0),
                str(card.get("card_id")),
                0 if layer == "outline" else 1,
            )

        prepared.sort(key=sort_key)
        changed = 0
        for display_order, (card, layer, progress, unit_type) in enumerate(prepared):
            card_id = str(card["card_id"])
            revision = int(card.get("revision_no") or card.get("current_revision_no") or 0)
            existing = cls._unit_existing(db, study_date, card_id, layer)
            if existing:
                if int(existing.get("content_revision_no") or 0) != revision:
                    cls._mark_stale_unit(db, existing, "CONTENT_CHANGED")
                    changed += 1
                # Never revive an explicitly invalid unit in the same day: the
                # client must refresh and start the next day's content version.
                continue
            unit_id = _new_id()
            try:
                db.execute(
                    """
                    INSERT INTO learning_day_units
                      (id, learning_day_id, study_date, day_unit_id, card_id, chapter_id, layer,
                       progress_id, card_revision_id, content_revision_no, unit_type, display_order,
                       position, started_at, invalid_reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                    """,
                    (unit_id, day.get("id"), study_date.isoformat(), unit_id, card_id,
                     card.get("chapter_id"), layer, progress.get("id"), card.get("revision_id"),
                     revision, unit_type, display_order, display_order),
                )
            except sqlite3.IntegrityError:
                existing = cls._unit_existing(db, study_date, card_id, layer)
                if existing:
                    continue
                raise
            unit = cls._unit_existing(db, study_date, card_id, layer)
            if unit:
                # Store display_order for deterministic retrieval when the
                # migration includes this optional column.  Older exact schema
                # omits it, so ignore only that one compatibility failure.
                try:
                    db.execute(
                        "UPDATE learning_day_units SET display_order = ? WHERE id = ?",
                        (display_order, unit_id),
                    )
                except sqlite3.OperationalError:
                    pass
                cls._insert_attempt(db, {**unit, "display_order": display_order}, int(day.get("current_round") or 1))
                changed += 1
        return changed

    @classmethod
    def _day(cls, db: sqlite3.Connection, study_date: date) -> Optional[dict[str, Any]]:
        return _one(db, "SELECT * FROM learning_days WHERE study_date = ?", (study_date.isoformat(),))

    @classmethod
    def _ensure_day(cls, db: sqlite3.Connection, study_date: date) -> tuple[dict[str, Any], bool]:
        day = cls._day(db, study_date)
        if day:
            return day, False
        try:
            db.execute(
                """
                INSERT INTO learning_days
                  (id, study_date, day_date, current_round, status, override_enabled, state_version,
                   created_at, updated_at)
                VALUES (?, ?, ?, 1, 'active', 0, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (_new_id(), study_date.isoformat(), study_date.isoformat()),
            )
        except sqlite3.IntegrityError:
            pass
        day = cls._day(db, study_date)
        if not day:
            raise LearningServiceError("SCHEMA_ERROR", "学习日无法创建", 500)
        return day, True

    @classmethod
    def _attempts_for_unit(cls, db: sqlite3.Connection, unit_id: str) -> list[dict[str, Any]]:
        return _rows(
            db,
            "SELECT * FROM learning_attempts WHERE day_unit_id = ? ORDER BY round ASC, attempt_index ASC",
            (unit_id,),
        )

    @classmethod
    def _unit_rows(cls, db: sqlite3.Connection, study_date: date) -> list[dict[str, Any]]:
        rows = _rows(
            db,
            """
            SELECT u.*, rc.knowledge_point_id, rc.chapter_id,
                   rc.current_revision_no AS card_current_revision_no,
                   COALESCE(NULLIF(pr.revision_no, 0), pr.revision_number) AS card_published_revision_no,
                   rc.is_paused AS card_is_paused,
                   rc.is_archived AS card_is_archived,
                   ch.title AS chapter_title,
                   COALESCE(rr.question, rr.prompt, rr.front_content) AS question,
                   COALESCE(rr.answer, rr.back_content) AS answer,
                   rr.card_type, rr.outline_points_json,
                   rr.explanation, rr.expanded_answer, rr.citations_json,
                    rr.hint, rr.source_excerpt,
                   rr.source_status, rr.review_status
            FROM learning_day_units u
            JOIN recall_cards rc ON rc.id = u.card_id
            JOIN recall_chapters ch ON ch.id = rc.chapter_id
            LEFT JOIN recall_card_revisions rr
              ON rr.card_id = rc.id AND rr.revision_no = u.content_revision_no
            LEFT JOIN recall_card_revisions pr ON pr.id = rc.published_revision_id
            WHERE u.study_date = ?
            ORDER BY
              CASE u.unit_type WHEN 'carryover' THEN 0 WHEN 'review' THEN 1 ELSE 2 END,
              u.id ASC
            """,
            (study_date.isoformat(),),
        )
        for row in rows:
            row["attempts"] = cls._attempts_for_unit(db, str(row["id"]))
            row["progress"] = cls._progress(db, str(row["card_id"]), str(row["layer"]))
        return rows

    @classmethod
    def _unit_public(cls, unit: Mapping[str, Any], *, revealed: bool = False, attempt: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        progress = unit.get("progress")
        if isinstance(progress, Mapping):
            progress_public = dict(progress)
        else:
            progress_public = None
        result: dict[str, Any] = {
            "id": unit.get("id"),
            "day_unit_id": unit.get("id"),
            "study_date": str(unit.get("study_date")),
            "card_id": unit.get("card_id"),
            "knowledge_point_id": unit.get("knowledge_point_id"),
            "chapter_id": unit.get("chapter_id"),
            "chapter_title": unit.get("chapter_title"),
            "layer": unit.get("layer"),
            "content_revision_no": int(unit.get("content_revision_no") or 0),
            "unit_type": unit.get("unit_type"),
            "position": int(unit.get("position") or unit.get("display_order") or 0) + 1,
            "total": int(unit.get("total") or 0),
            "started_at": unit.get("started_at"),
            "invalid_reason": unit.get("invalid_reason"),
            "progress": progress_public,
            "revealed": bool(revealed),
        }
        if attempt is not None:
            result["attempt"] = dict(attempt)
            result["attempt_id"] = attempt.get("id")
            result["round"] = attempt.get("round")
            result["attempt_index"] = attempt.get("attempt_index")
        # The prompt is safe before reveal.  Explanations/answers/citations are
        # intentionally omitted until reveal to preserve recall integrity.
        result["question"] = unit.get("question") or ""
        result["card_type"] = unit.get("card_type")
        result["hint"] = unit.get("hint") or unit.get("cue")
        result["source_excerpt"] = unit.get("source_excerpt")
        if revealed:
            result["answer"] = (
                unit.get("answer")
                or unit.get("expanded_answer")
                or unit.get("explanation")
                or ""
            )
            result["outline_points"] = _loads(unit.get("outline_points_json"), [])
            result["explanation"] = unit.get("explanation")
            result["expanded_answer"] = unit.get("expanded_answer") or unit.get("answer")
            result["citations"] = _loads(unit.get("citations_json"), [])
            result["source_status"] = unit.get("source_status")
            result["review_status"] = unit.get("review_status")
        return result

    @classmethod
    def _summary(cls, db: sqlite3.Connection, study_date: date) -> dict[str, Any]:
        day = cls._day(db, study_date)
        settings = cls.get_settings(conn=db)
        if not day:
            return {
                "study_date": study_date.isoformat(),
                "status": "not_open",
                "opened": False,
                "current_round": None,
                "override_enabled": False,
                "state_version": None,
                "units": [],
                "counts": {"total": 0, "pending": 0, "completed": 0, "new": 0, "review": 0, "carryover": 0},
                "settings": settings,
                "next_item": None,
            }
        rows = cls._unit_rows(db, study_date)
        current_round = int(day.get("current_round") or 1)
        active_rows = [
            row for row in rows
            if not row.get("invalid_reason")
            and not _as_bool(row.get("card_is_paused"))
            and str(row.get("source_status") or "valid").lower() not in {"invalid", "missing", "rejected", "deleted"}
        ]
        pending = 0
        completed = 0
        counts = {"total": len(active_rows), "pending": 0, "completed": 0, "new": 0, "review": 0, "carryover": 0}
        unit_summaries: list[dict[str, Any]] = []
        for row in active_rows:
            kind = str(row.get("unit_type") or "review")
            if kind in counts:
                counts[kind] += 1
            attempts = row.get("attempts") or []
            current = [a for a in attempts if int(a.get("round") or 0) == current_round]
            unresolved = any(str(a.get("status") or "pending") in {"pending", "revealed"} for a in current)
            # Review units only participate in round one; new and carryover units
            # participate in rounds they are scheduled for.
            if kind == "review" and current_round > 1:
                unresolved = False
            if unresolved:
                pending += 1
                counts["pending"] += 1
            else:
                completed += 1
                counts["completed"] += 1
            unit_summaries.append(cls._unit_public(row, revealed=False))
        next_item = None
        if day.get("status") == "active":
            for row in active_rows:
                if str(row.get("unit_type")) == "review" and current_round > 1:
                    continue
                attempts = [a for a in row.get("attempts", []) if int(a.get("round") or 0) == current_round]
                attempts.sort(key=lambda a: int(a.get("attempt_index") or 0))
                candidate = next((a for a in attempts if str(a.get("status") or "pending") in {"pending", "revealed"}), None)
                if candidate:
                    next_item = cls._unit_public(row, revealed=False, attempt=candidate)
                    break
        return {
            "study_date": study_date.isoformat(),
            "status": day.get("status"),
            "opened": True,
            "current_round": current_round,
            "override_enabled": bool(day.get("override_enabled")),
            "state_version": int(day.get("state_version") or 1),
            "day_version": int(day.get("state_version") or 1),
            "units": unit_summaries,
            "counts": counts,
            "settings": settings,
            "next_item": next_item,
        }

    # -- day lifecycle ---------------------------------------------------

    @classmethod
    def get_today(
        cls,
        *args: Any,
        study_date: Any = None,
        conn: Optional[sqlite3.Connection] = None,
        now: Any = None,
        current_date: Any = None,
        enforce_today: Optional[bool] = None,
    ) -> dict[str, Any]:
        conn, study_date = _coerce_connection_date(args, conn, study_date)
        supplied = study_date is not None
        day_date = resolve_study_date(study_date, now=now)
        cls._check_cross_day(
            day_date,
            study_date_supplied=supplied,
            now=now,
            current_date=current_date,
            enforce_today=enforce_today,
        )
        with _connection(conn) as (db, _owned):
            return cls._summary(db, day_date)

    @classmethod
    def open_today(
        cls,
        *args: Any,
        study_date: Any = None,
        conn: Optional[sqlite3.Connection] = None,
        now: Any = None,
        current_date: Any = None,
        enforce_today: Optional[bool] = None,
        override: Optional[bool] = None,
        resume: bool = False,
    ) -> dict[str, Any]:
        conn, study_date = _coerce_connection_date(args, conn, study_date)
        supplied = study_date is not None
        day_date = resolve_study_date(study_date, now=now, clock=getattr(cls, "clock", None))
        cls._check_cross_day(
            day_date,
            study_date_supplied=supplied,
            now=now,
            current_date=current_date,
            enforce_today=enforce_today,
        )
        with _connection(conn) as (db, _owned):
            with _write_tx(db):
                day, created = cls._ensure_day(db, day_date)
                if resume and day.get("status") == "paused":
                    db.execute(
                        "UPDATE learning_days SET status = 'active', state_version = COALESCE(state_version, 1) + 1, updated_at = CURRENT_TIMESTAMP WHERE study_date = ?",
                        (day_date.isoformat(),),
                    )
                    day = cls._day(db, day_date) or day
                if override is not None and bool(override) != bool(day.get("override_enabled")):
                    db.execute(
                        "UPDATE learning_days SET override_enabled = ?, state_version = COALESCE(state_version, 1) + 1, updated_at = CURRENT_TIMESTAMP WHERE study_date = ?",
                        (1 if override else 0, day_date.isoformat()),
                    )
                    day = cls._day(db, day_date) or day
                before = int(day.get("state_version") or 1)
                added = cls._build_units(db, day, day_date, force=bool(override))
                # Existing-day rebuilds are observable through optimistic
                # versioning; first open remains version 1 for stable clients.
                if not created and added:
                    db.execute(
                        "UPDATE learning_days SET state_version = COALESCE(state_version, 1) + 1, updated_at = CURRENT_TIMESTAMP WHERE study_date = ?",
                        (day_date.isoformat(),),
                    )
            return cls._summary(db, day_date)

    @classmethod
    def _load_day_or_error(cls, db: sqlite3.Connection, study_date: date) -> dict[str, Any]:
        day = cls._day(db, study_date)
        if not day:
            raise LearningServiceError("DAY_NOT_OPEN", "今日学习尚未开始", 404)
        return day

    @classmethod
    def _assert_unit_actionable(cls, unit: Mapping[str, Any]) -> None:
        """Reject actions on units whose card is no longer learnable.

        Pause/archive and source invalidation are *qualifications*, not learning
        states: an already-built day unit must stop being actionable the moment
        the underlying card is paused, archived, or its source becomes invalid.
        """
        if _as_bool(unit.get("card_is_archived")):
            raise LearningServiceError("CARD_ARCHIVED", "该学习卡已归档，无法继续学习", 409)
        if _as_bool(unit.get("card_is_paused")):
            raise LearningServiceError("CARD_PAUSED", "该学习卡已暂停，恢复后才能继续学习", 409)
        source_status = str(unit.get("source_status") or "").strip().lower()
        if source_status in {"invalid", "missing", "rejected", "deleted"}:
            raise LearningServiceError("SOURCE_INVALID", "该学习卡来源已失效，请刷新内容", 409)
        review_status = str(unit.get("review_status") or "").strip().lower()
        if review_status in {"rejected", "invalid", "deleted", "draft"}:
            raise LearningServiceError("SOURCE_INVALID", "该学习卡内容未通过审核，请刷新内容", 409)

    @classmethod
    def _find_unit(
        cls,
        db: sqlite3.Connection,
        study_date: date,
        *,
        unit_id: Optional[str] = None,
        card_id: Optional[str] = None,
        layer: Optional[str] = None,
    ) -> dict[str, Any]:
        if layer is not None and layer not in LAYERS:
            raise LearningServiceError("INVALID_LAYER", "layer 必须是 outline 或 expanded", 422)
        if unit_id:
            unit = _one(db, "SELECT * FROM learning_day_units WHERE id = ? AND study_date = ?", (unit_id, study_date.isoformat()))
        elif card_id and layer:
            unit = _one(
                db,
                "SELECT * FROM learning_day_units WHERE card_id = ? AND layer = ? AND study_date = ?",
                (card_id, layer, study_date.isoformat()),
            )
        else:
            raise LearningServiceError("UNIT_REQUIRED", "必须提供 day_unit_id 或 card_id+layer", 422)
        if not unit:
            raise LearningServiceError("UNIT_NOT_FOUND", "学习单元不存在", 404)
        enriched = _one(
            db,
            """
            SELECT u.*, rc.knowledge_point_id, rc.chapter_id,
                   rc.current_revision_no AS card_current_revision_no,
                   rc.is_paused AS card_is_paused,
                   rc.is_archived AS card_is_archived,
                   ch.title AS chapter_title,
                   COALESCE(rr.question, rr.prompt, rr.front_content) AS question,
                   COALESCE(rr.answer, rr.back_content) AS answer,
                   rr.card_type, rr.outline_points_json,
                   rr.explanation, rr.expanded_answer, rr.citations_json,
                    rr.hint, rr.source_excerpt,
                   rr.source_status, rr.review_status
            FROM learning_day_units u
            JOIN recall_cards rc ON rc.id = u.card_id
            JOIN recall_chapters ch ON ch.id = rc.chapter_id
            LEFT JOIN recall_card_revisions rr ON rr.card_id = rc.id AND rr.revision_no = u.content_revision_no
            WHERE u.id = ?
            """,
            (unit["id"],),
        )
        if enriched is None:
            raise LearningServiceError("UNIT_NOT_FOUND", "学习单元对应内容不存在", 404)
        enriched["progress"] = cls._progress(db, str(enriched["card_id"]), str(enriched["layer"]))
        enriched["attempts"] = cls._attempts_for_unit(db, str(enriched["id"]))
        return enriched

    @classmethod
    def _find_attempt(
        cls,
        db: sqlite3.Connection,
        unit: Mapping[str, Any],
        round_no: int,
        *,
        attempt_id: Optional[str] = None,
        attempt_index: Optional[int] = None,
    ) -> dict[str, Any]:
        if attempt_id:
            attempt = _one(db, "SELECT * FROM learning_attempts WHERE id = ? AND day_unit_id = ?", (attempt_id, unit["id"]))
        else:
            if attempt_index is None:
                # Prefer the pending/revealed attempt, which is the primary
                # unless it has produced the additional forgotten retry.
                attempt = _one(
                    db,
                    """
                    SELECT * FROM learning_attempts
                    WHERE day_unit_id = ? AND round = ? AND status IN ('pending', 'revealed')
                    ORDER BY attempt_index ASC LIMIT 1
                    """,
                    (unit["id"], round_no),
                )
            else:
                attempt = _one(
                    db,
                    "SELECT * FROM learning_attempts WHERE day_unit_id = ? AND round = ? AND attempt_index = ?",
                    (unit["id"], round_no, attempt_index),
                )
        if not attempt:
            raise LearningServiceError("ATTEMPT_NOT_FOUND", "本轮学习尝试不存在", 404)
        if int(attempt.get("round") or 0) != round_no:
            raise LearningServiceError("ROUND_CONFLICT", "学习尝试不属于当前轮次", 409)
        return attempt

    @classmethod
    def next_item(
        cls,
        *args: Any,
        study_date: Any = None,
        conn: Optional[sqlite3.Connection] = None,
        day_unit_id: Optional[str] = None,
        unit_id: Optional[str] = None,
        day_version: Any = None,
        state_version: Any = None,
        now: Any = None,
        current_date: Any = None,
        enforce_today: Optional[bool] = None,
    ) -> Optional[dict[str, Any]]:
        conn, study_date = _coerce_connection_date(args, conn, study_date)
        day_date = resolve_study_date(study_date, now=now)
        cls._check_cross_day(day_date, study_date_supplied=study_date is not None, now=now, current_date=current_date, enforce_today=enforce_today)
        expected_version = day_version if day_version is not None else state_version
        with _connection(conn) as (db, _owned):
            day = cls._load_day_or_error(db, day_date)
            cls._check_day_version(day, expected_version)
            if day.get("status") == "paused":
                raise LearningServiceError("DAY_PAUSED", "今日学习已暂停", 409)
            if day.get("status") in {"completed", "archived"}:
                return {"done": True, "status": day.get("status"), "study_date": day_date.isoformat(), "day_version": int(day.get("state_version") or 1)}
            current_round = int(day.get("current_round") or 1)
            rows = cls._unit_rows(db, day_date)
            for unit in rows:
                if unit.get("invalid_reason"):
                    continue
                if _as_bool(unit.get("card_is_paused")) or _as_bool(unit.get("card_is_archived")):
                    continue
                if str(unit.get("source_status") or "").strip().lower() in {"invalid", "missing", "rejected", "deleted"}:
                    continue
                if str(unit.get("unit_type")) == "review" and current_round > 1:
                    continue
                attempts = [a for a in unit.get("attempts", []) if int(a.get("round") or 0) == current_round]
                attempts.sort(key=lambda a: int(a.get("attempt_index") or 0))
                attempt = next((a for a in attempts if str(a.get("status") or "pending") in {"pending", "revealed"}), None)
                if attempt:
                    if not unit.get("started_at"):
                        try:
                            db.execute("UPDATE learning_day_units SET started_at = CURRENT_TIMESTAMP WHERE id = ?", (unit["id"],))
                            unit["started_at"] = _utc_now_iso()
                        except sqlite3.OperationalError:
                            pass
                    return cls._unit_public(unit, revealed=False, attempt=attempt)
            return {"done": True, "status": day.get("status"), "study_date": day_date.isoformat(), "current_round": current_round, "day_version": int(day.get("state_version") or 1)}

    @classmethod
    def override(
        cls,
        *args: Any,
        study_date: Any = None,
        conn: Optional[sqlite3.Connection] = None,
        enabled: Optional[bool] = None,
        day_version: Any = None,
        state_version: Any = None,
        now: Any = None,
        current_date: Any = None,
        enforce_today: Optional[bool] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        conn, study_date = _coerce_connection_date(args, conn, study_date)
        day_date = resolve_study_date(study_date, now=now)
        cls._check_cross_day(day_date, study_date_supplied=study_date is not None, now=now, current_date=current_date, enforce_today=enforce_today)
        expected = day_version if day_version is not None else state_version
        if enabled is None:
            enabled = _as_bool(_first(kwargs, "override_enabled", "value", default=True))
        with _connection(conn) as (db, _owned):
            with _write_tx(db):
                day = cls._load_day_or_error(db, day_date)
                cls._check_day_version(day, expected)
                if day.get("status") != "active":
                    raise LearningServiceError("DAY_NOT_ACTIVE", "当前学习日不可修改", 409)
                if bool(enabled) != bool(day.get("override_enabled")):
                    db.execute(
                        """
                        UPDATE learning_days
                        SET override_enabled = ?, state_version = COALESCE(state_version, 1) + 1,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE study_date = ?
                        """,
                        (1 if enabled else 0, day_date.isoformat()),
                    )
                    day = cls._day(db, day_date) or day
                added = cls._build_units(db, day, day_date, force=bool(enabled))
                if added and bool(enabled) == bool(day.get("override_enabled")):
                    # If the toggle itself already incremented the version, one
                    # version bump is enough to cover rebuilding; otherwise make
                    # additions visible to stale clients.
                    if expected is not None and int(day.get("state_version") or 1) == int(expected):
                        db.execute(
                            "UPDATE learning_days SET state_version = COALESCE(state_version, 1) + 1, updated_at = CURRENT_TIMESTAMP WHERE study_date = ?",
                            (day_date.isoformat(),),
                        )
            return cls._summary(db, day_date)

    @classmethod
    def reveal(
        cls,
        *args: Any,
        study_date: Any = None,
        conn: Optional[sqlite3.Connection] = None,
        day_unit_id: Optional[str] = None,
        unit_id: Optional[str] = None,
        card_id: Optional[str] = None,
        layer: Optional[str] = None,
        attempt_id: Optional[str] = None,
        round: Optional[int] = None,
        attempt_index: Optional[int] = None,
        day_version: Any = None,
        state_version: Any = None,
        content_revision_no: Any = None,
        now: Any = None,
        current_date: Any = None,
        enforce_today: Optional[bool] = None,
    ) -> dict[str, Any]:
        conn, study_date = _coerce_connection_date(args, conn, study_date)
        day_date = resolve_study_date(study_date, now=now)
        cls._check_cross_day(day_date, study_date_supplied=study_date is not None, now=now, current_date=current_date, enforce_today=enforce_today)
        unit_id = day_unit_id or unit_id
        with _connection(conn) as (db, _owned):
            with _write_tx(db):
                day = cls._load_day_or_error(db, day_date)
                cls._check_day_version(day, day_version if day_version is not None else state_version)
                if day.get("status") != "active":
                    raise LearningServiceError("DAY_NOT_ACTIVE", "当前学习日不可操作", 409)
                # Attempt routes identify the attempt, while the service's unit
                # lookup is keyed by day unit. Resolve that relationship before
                # enforcing the unit selector contract.
                if not unit_id and attempt_id:
                    attempt_row = _one(db, "SELECT day_unit_id FROM learning_attempts WHERE id = ?", (attempt_id,))
                    unit_id = attempt_row.get("day_unit_id") if attempt_row else None
                unit = cls._find_unit(db, day_date, unit_id=unit_id, card_id=card_id, layer=layer)
                if unit.get("invalid_reason"):
                    raise LearningServiceError("UNIT_INVALID", "该学习单元已失效，请刷新内容", 409)
                cls._assert_unit_actionable(unit)
                cls._check_content_version(unit, content_revision_no)
                current_revision = int(unit.get("card_current_revision_no") or 0)
                if current_revision != int(unit.get("content_revision_no") or 0):
                    cls._mark_stale_unit(db, unit, "CONTENT_CHANGED")
                    raise LearningServiceError("CONTENT_VERSION_CONFLICT", "学习卡内容已更新，请刷新后重试", 409)
                round_no = _as_int(round if round is not None else day.get("current_round") or 1, "round", minimum=1, maximum=NEW_CARD_ROUNDS)
                attempt = cls._find_attempt(db, unit, round_no, attempt_id=attempt_id, attempt_index=attempt_index)
                status = str(attempt.get("status") or "pending")
                if status == "invalid":
                    raise LearningServiceError("ATTEMPT_INVALID", "该尝试已失效", 409)
                if status == "pending":
                    db.execute(
                        "UPDATE learning_attempts SET revealed_at = CURRENT_TIMESTAMP, status = 'revealed' WHERE id = ?",
                        (attempt["id"],),
                    )
                    attempt = _one(db, "SELECT * FROM learning_attempts WHERE id = ?", (attempt["id"],)) or attempt
                return cls._unit_public(unit, revealed=True, attempt=attempt)

    @classmethod
    def _normalise_rating(cls, rating: Any) -> str:
        if not isinstance(rating, str):
            raise LearningServiceError("INVALID_RATING", "rating 必须是 forgotten、fuzzy 或 mastered", 422)
        value = rating.strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {"forgot": "forgotten", "forget": "forgotten", "hard": "fuzzy", "uncertain": "fuzzy", "easy": "mastered", "master": "mastered"}
        value = aliases.get(value, value)
        if value not in RATINGS:
            raise LearningServiceError("INVALID_RATING", "rating 必须是 forgotten、fuzzy 或 mastered", 422)
        return value

    @classmethod
    def _event_response(cls, db: sqlite3.Connection, request_id: str, fingerprint: str) -> Optional[dict[str, Any]]:
        event = _one(db, "SELECT * FROM learning_events WHERE request_id = ?", (request_id,))
        if not event:
            return None
        old_fp = event.get("request_fingerprint")
        if old_fp and old_fp != fingerprint:
            raise LearningServiceError("IDEMPOTENCY_CONFLICT", "request_id 已用于另一项请求", 409)
        response = _loads(event.get("response_json"), None)
        if isinstance(response, dict):
            response["idempotent_replay"] = True
            return response
        return {"request_id": request_id, "idempotent_replay": True}

    @classmethod
    def rate(
        cls,
        *args: Any,
        study_date: Any = None,
        conn: Optional[sqlite3.Connection] = None,
        day_unit_id: Optional[str] = None,
        unit_id: Optional[str] = None,
        card_id: Optional[str] = None,
        layer: Optional[str] = None,
        attempt_id: Optional[str] = None,
        round: Optional[int] = None,
        attempt_index: Optional[int] = None,
        rating: Any = None,
        request_id: Optional[str] = None,
        request_fingerprint: Optional[str] = None,
        day_version: Any = None,
        state_version: Any = None,
        content_revision_no: Any = None,
        now: Any = None,
        current_date: Any = None,
        enforce_today: Optional[bool] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        conn, study_date = _coerce_connection_date(args, conn, study_date)
        # A request object can be passed as the first non-connection positional
        # argument by a few router drafts; merge it before validation.
        if isinstance(rating, Mapping) or hasattr(rating, "model_dump"):
            body = _mapping(rating)
            rating = _first(body, "rating", "value")
            request_id = request_id or _first(body, "request_id", "idempotency_key")
            day_unit_id = day_unit_id or _first(body, "day_unit_id", "unit_id")
            attempt_id = attempt_id or _first(body, "attempt_id")
            layer = layer or _first(body, "layer")
            card_id = card_id or _first(body, "card_id")
            content_revision_no = content_revision_no if content_revision_no is not None else _first(body, "content_revision_no")
            day_version = day_version if day_version is not None else _first(body, "day_version", "state_version")
        day_date = resolve_study_date(study_date, now=now)
        cls._check_cross_day(day_date, study_date_supplied=study_date is not None, now=now, current_date=current_date, enforce_today=enforce_today)
        canonical_rating = cls._normalise_rating(rating)
        unit_id = day_unit_id or unit_id
        request_id = request_id or _first(kwargs, "idempotency_key", "requestId")
        expected_version = day_version if day_version is not None else state_version
        # Include exactly the state-changing request attributes in the default
        # fingerprint; connection/date hooks are not part of application data.
        fp_payload = {
            "study_date": day_date.isoformat(),
            "day_unit_id": unit_id,
            "card_id": card_id,
            "layer": layer,
            "attempt_id": attempt_id,
            "round": round,
            "attempt_index": attempt_index,
            "rating": canonical_rating,
            "content_revision_no": content_revision_no,
        }
        fingerprint = request_fingerprint or _fingerprint(fp_payload)
        if request_id is None:
            # A stable synthetic id is not safe across distinct requests.  We
            # still use a UUID for the event's NOT NULL/UNIQUE contract and
            # return it to callers, while repeated submissions are guarded by
            # the attempt's terminal state below.
            request_id = _new_id()
        with _connection(conn) as (db, _owned):
            with _write_tx(db):
                replay = cls._event_response(db, str(request_id), fingerprint)
                if replay is not None:
                    return replay
                day = cls._load_day_or_error(db, day_date)
                cls._check_day_version(day, expected_version)
                if day.get("status") != "active":
                    raise LearningServiceError("DAY_NOT_ACTIVE", "当前学习日不可操作", 409)
                # Resolve the unit from the route's attempt id when the client
                # does not repeat day_unit_id in the request body.
                if not unit_id and attempt_id:
                    attempt_row = _one(db, "SELECT day_unit_id FROM learning_attempts WHERE id = ?", (attempt_id,))
                    unit_id = attempt_row.get("day_unit_id") if attempt_row else None
                unit = cls._find_unit(db, day_date, unit_id=unit_id, card_id=card_id, layer=layer)
                if unit.get("invalid_reason"):
                    raise LearningServiceError("UNIT_INVALID", "该学习单元已失效，请刷新内容", 409)
                cls._assert_unit_actionable(unit)
                cls._check_content_version(unit, content_revision_no)
                if int(unit.get("card_current_revision_no") or 0) != int(unit.get("content_revision_no") or 0):
                    cls._mark_stale_unit(db, unit, "CONTENT_CHANGED")
                    raise LearningServiceError("CONTENT_VERSION_CONFLICT", "学习卡内容已更新，请刷新后重试", 409)
                round_no = _as_int(round if round is not None else day.get("current_round") or 1, "round", minimum=1, maximum=NEW_CARD_ROUNDS)
                attempt = cls._find_attempt(db, unit, round_no, attempt_id=attempt_id, attempt_index=attempt_index)
                attempt_status = str(attempt.get("status") or "pending")
                if attempt_status == "invalid":
                    raise LearningServiceError("ATTEMPT_INVALID", "该尝试已失效", 409)
                if attempt_status == "pending":
                    raise LearningServiceError("REVEAL_REQUIRED", "请先揭示答案再评分", 409)
                if attempt_status == "rated":
                    if attempt.get("rating") == canonical_rating:
                        return {
                            "request_id": request_id,
                            "idempotent_replay": True,
                            "study_date": day_date.isoformat(),
                            "day_version": int(day.get("state_version") or 1),
                            "attempt": dict(attempt),
                            "unit": cls._unit_public(unit, revealed=True, attempt=attempt),
                        }
                    raise LearningServiceError("ATTEMPT_ALREADY_RATED", "该尝试已经评分", 409)

                progress_before = cls._progress(db, str(unit["card_id"]), str(unit["layer"]))
                if progress_before is None:
                    progress_before = cls._ensure_progress(
                        db,
                        str(unit["card_id"]),
                        str(unit["layer"]),
                        int(unit["content_revision_no"]),
                        day_date,
                        chapter_id=str(unit.get("chapter_id") or ""),
                        card_revision_id=str(unit.get("card_revision_id") or unit.get("revision_id") or "") or None,
                    )
                before_snapshot = {
                    "day": {"study_date": day_date.isoformat(), "current_round": int(day.get("current_round") or 1), "state_version": int(day.get("state_version") or 1)},
                    "unit": {"id": unit.get("id"), "content_revision_no": unit.get("content_revision_no"), "unit_type": unit.get("unit_type")},

                    "attempt": dict(attempt),
                    "progress": dict(progress_before),
                }
                state_input = dict(progress_before)
                # DB stores nonnegative interval indexes for new/learning;
                # scheduler accepts the explicit state and keeps new rounds.
                try:
                    if int(attempt.get("attempt_index") or 0) > 0:
                        # Same-day additional recall is a neutral recap: it must
                        # never advance the interval ladder or move the due date.
                        after_state = dict(state_input)
                        after_state["same_day_additional"] = True
                        after_state["last_rating"] = canonical_rating
                        after_state["status"] = str(
                            progress_before.get("status") or progress_before.get("state") or STATUS_NEW
                        )
                        after_state["interval_index"] = int(progress_before.get("interval_index") or 0)
                        after_state["interval_days"] = int(progress_before.get("interval_days") or 0)
                        after_state["new_round"] = int(progress_before.get("new_round") or 0)
                        after_state["next_review_date"] = (
                            progress_before.get("due_date") or day_date.isoformat()
                        )
                    else:
                        after_state = transition_state(state_input, canonical_rating, review_date=day_date)
                except (TypeError, ValueError) as exc:
                    raise LearningServiceError("INVALID_PROGRESS", str(exc), 409) from exc
                after_state_name = str(after_state.get("status") or STATUS_NEW)
                after_interval = int(after_state.get("interval_index") or 0)
                if after_interval < 0:
                    after_interval = 0
                after_new_round = int(after_state.get("new_round") or 0)
                due = after_state.get("next_review_date") or day_date
                due_date = _parse_date(due, "due_date")
                first_study = progress_before.get("first_study_date") or day_date.isoformat()
                progress_version = int(progress_before.get("state_version") or 1) + 1
                previous_repetitions = int(progress_before.get("repetitions") or 0)
                previous_review_count = int(progress_before.get("review_count") or 0)
                repetitions = previous_repetitions + 1
                review_count = previous_review_count + 1
                interval_days = int(after_state.get("interval_days") or 0)
                now_stamp = _utc_now_iso()
                db.execute(
                    """
                    UPDATE learning_progress
                    SET chapter_id = COALESCE(chapter_id, ?), card_revision_id = ?,
                        content_revision_no = ?, state = ?, status = ?, interval_index = ?,
                        new_round = ?,
                        repetitions = ?, interval_days = ?, due_date = ?, due_at = ?,
                        first_study_date = ?, last_studied_at = ?, last_reviewed_at = ?,
                        review_count = ?, state_version = ?, version = COALESCE(version, 1) + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE card_id = ? AND layer = ?
                    """,
                    (
                        unit.get("chapter_id"), unit.get("card_revision_id"),
                        int(unit["content_revision_no"]), after_state_name, after_state_name,
                        after_interval, after_new_round, repetitions, interval_days, due_date.isoformat(),
                        due_date.isoformat() + "T00:00:00+00:00", str(first_study), now_stamp,
                        now_stamp, review_count, progress_version,
                        unit["card_id"], unit["layer"],
                    ),
                )
                db.execute(
                    """
                    UPDATE learning_attempts
                    SET rating = ?, submitted_at = CURRENT_TIMESTAMP, status = 'rated'
                    WHERE id = ?
                    """,
                    (canonical_rating, attempt["id"]),
                )
                attempt_after = _one(db, "SELECT * FROM learning_attempts WHERE id = ?", (attempt["id"],)) or {**attempt, "rating": canonical_rating, "status": "rated"}
                additional: Optional[dict[str, Any]] = None
                if canonical_rating == "forgotten" and int(attempt.get("attempt_index") or 0) == 0:
                    additional = cls._insert_attempt(
                        db,
                        unit,
                        round_no,
                        attempt_kind="additional",
                        attempt_index=1,
                        display_order=int(attempt.get("display_order") or 0) + 1,
                    )
                db.execute(
                    "UPDATE learning_days SET state_version = COALESCE(state_version, 1) + 1, updated_at = CURRENT_TIMESTAMP WHERE study_date = ?",
                    (day_date.isoformat(),),
                )
                day_after = cls._day(db, day_date) or day
                progress_after = cls._progress(db, str(unit["card_id"]), str(unit["layer"])) or {}
                after_snapshot = {
                    "day": {"study_date": day_date.isoformat(), "current_round": int(day_after.get("current_round") or 1), "state_version": int(day_after.get("state_version") or 1)},
                    "attempt": dict(attempt_after),
                    "additional_attempt": dict(additional) if additional else None,
                    "progress": dict(progress_after),
                }
                response: dict[str, Any] = {
                    "request_id": request_id,
                    "idempotent_replay": False,
                    "study_date": day_date.isoformat(),
                    "day_version": int(day_after.get("state_version") or 1),
                    "state_version": int(day_after.get("state_version") or 1),
                    "round": round_no,
                    "rating": canonical_rating,
                    "attempt": dict(attempt_after),
                    "additional_attempt": dict(additional) if additional else None,
                    "progress": dict(progress_after),
                    "unit": cls._unit_public(unit, revealed=True, attempt=attempt_after),
                }
                try:
                    db.execute(
                        """
                        INSERT INTO learning_events
                          (request_id, request_fingerprint, event_type, entity_type, entity_id,
                           day_unit_id, study_date, card_id, chapter_id, learning_day_id, layer,
                           content_revision_no, rating, before_snapshot_json,
                           after_snapshot_json, response_json, created_at)
                        VALUES (?, ?, 'attempt_rated', 'attempt', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        """,
                        (
                            str(request_id), fingerprint, attempt["id"], unit["id"], day_date.isoformat(),
                            unit["card_id"], unit.get("chapter_id"), unit.get("learning_day_id"), unit["layer"],
                            int(unit["content_revision_no"]), canonical_rating,
                            _json(before_snapshot), _json(after_snapshot), _json(response),
                        ),
                    )
                except sqlite3.IntegrityError:
                    replay = cls._event_response(db, str(request_id), fingerprint)
                    if replay is not None:
                        return replay
                    raise LearningServiceError("IDEMPOTENCY_CONFLICT", "该请求已被处理", 409)
                return response

    @classmethod
    def _current_round_complete(cls, db: sqlite3.Connection, day: Mapping[str, Any], study_date: date) -> bool:
        current_round = int(day.get("current_round") or 1)
        for unit in cls._unit_rows(db, study_date):
            if unit.get("invalid_reason"):
                continue
            kind = str(unit.get("unit_type") or "review")
            if kind == "review" and current_round > 1:
                continue
            current = [a for a in unit.get("attempts", []) if int(a.get("round") or 0) == current_round]
            if not current:
                continue
            # A primary forgotten attempt must be followed by its additional
            # attempt; no other rating creates an additional attempt.
            primary = next((a for a in current if int(a.get("attempt_index") or 0) == 0), None)
            if primary is None or str(primary.get("status")) != "rated":
                return False
            if primary.get("rating") == "forgotten":
                additional = next((a for a in current if int(a.get("attempt_index") or 0) == 1), None)
                if additional is None or str(additional.get("status")) != "rated":
                    return False
        return True

    @classmethod
    def _needs_next_round(cls, db: sqlite3.Connection, unit: Mapping[str, Any], study_date: date) -> bool:
        if str(unit.get("unit_type")) not in {"new", "carryover"}:
            return False
        progress = cls._progress(db, str(unit["card_id"]), str(unit["layer"]))
        if not progress:
            return False
        due = progress.get("due_date")
        if not due:
            return True
        return _parse_date(due, "due_date") <= study_date and str(progress.get("state")) in {STATUS_NEW, STATUS_LEARNING}

    @classmethod
    def advance(
        cls,
        *args: Any,
        study_date: Any = None,
        conn: Optional[sqlite3.Connection] = None,
        day_version: Any = None,
        state_version: Any = None,
        now: Any = None,
        current_date: Any = None,
        enforce_today: Optional[bool] = None,
    ) -> dict[str, Any]:
        conn, study_date = _coerce_connection_date(args, conn, study_date)
        day_date = resolve_study_date(study_date, now=now)
        cls._check_cross_day(day_date, study_date_supplied=study_date is not None, now=now, current_date=current_date, enforce_today=enforce_today)
        expected = day_version if day_version is not None else state_version
        with _connection(conn) as (db, _owned):
            with _write_tx(db):
                day = cls._load_day_or_error(db, day_date)
                cls._check_day_version(day, expected)
                if day.get("status") == "paused":
                    raise LearningServiceError("DAY_PAUSED", "今日学习已暂停", 409)
                if day.get("status") in {"completed", "archived"}:
                    return cls._summary(db, day_date)
                if not cls._current_round_complete(db, day, day_date):
                    raise LearningServiceError("ROUND_NOT_COMPLETE", "当前轮次仍有未完成的学习尝试", 409)
                current_round = int(day.get("current_round") or 1)
                rows = cls._unit_rows(db, day_date)
                continuation = current_round < NEW_CARD_ROUNDS and any(
                    cls._needs_next_round(db, unit, day_date) for unit in rows if not unit.get("invalid_reason")
                )
                if continuation:
                    next_round = current_round + 1
                    db.execute(
                        "UPDATE learning_days SET current_round = ?, state_version = COALESCE(state_version, 1) + 1, updated_at = CURRENT_TIMESTAMP WHERE study_date = ?",
                        (next_round, day_date.isoformat()),
                    )
                    # New and carryover units take all needed rounds; review units are one-attempt daily reviews.
                    for unit in rows:
                        if unit.get("invalid_reason") or str(unit.get("unit_type")) not in {"new", "carryover"}:
                            continue
                        if cls._needs_next_round(db, unit, day_date):
                            cls._insert_attempt(db, unit, next_round, attempt_kind="primary", attempt_index=0)
                else:
                    db.execute(
                        "UPDATE learning_days SET status = 'completed', state_version = COALESCE(state_version, 1) + 1, updated_at = CURRENT_TIMESTAMP WHERE study_date = ?",
                        (day_date.isoformat(),),
                    )
            return cls._summary(db, day_date)

    @classmethod
    def pause(
        cls,
        *args: Any,
        study_date: Any = None,
        conn: Optional[sqlite3.Connection] = None,
        day_version: Any = None,
        state_version: Any = None,
        now: Any = None,
        current_date: Any = None,
        enforce_today: Optional[bool] = None,
    ) -> dict[str, Any]:
        conn, study_date = _coerce_connection_date(args, conn, study_date)
        day_date = resolve_study_date(study_date, now=now)
        cls._check_cross_day(day_date, study_date_supplied=study_date is not None, now=now, current_date=current_date, enforce_today=enforce_today)
        expected = day_version if day_version is not None else state_version
        with _connection(conn) as (db, _owned):
            with _write_tx(db):
                day = cls._load_day_or_error(db, day_date)
                cls._check_day_version(day, expected)
                if day.get("status") == "active":
                    db.execute(
                        "UPDATE learning_days SET status = 'paused', state_version = COALESCE(state_version, 1) + 1, updated_at = CURRENT_TIMESTAMP WHERE study_date = ?",
                        (day_date.isoformat(),),
                    )
            return cls._summary(db, day_date)


# ---------------------------------------------------------------------------
# Module-level aliases keep integration simple for function-style routers.
# ---------------------------------------------------------------------------


def get_settings(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return LearningService.get_settings(*args, **kwargs)


def update_settings(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return LearningService.update_settings(*args, **kwargs)


def save_scope(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return LearningService.save_scope(*args, **kwargs)


def list_chapters(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
    return LearningService.list_chapters(*args, **kwargs)


def create_chapter(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return LearningService.create_chapter(*args, **kwargs)


def update_chapter(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return LearningService.update_chapter(*args, **kwargs)


def get_today(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return LearningService.get_today(*args, **kwargs)


def open_today(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return LearningService.open_today(*args, **kwargs)


def next_item(*args: Any, **kwargs: Any) -> Optional[dict[str, Any]]:
    return LearningService.next_item(*args, **kwargs)


def override(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return LearningService.override(*args, **kwargs)


def reveal(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return LearningService.reveal(*args, **kwargs)


def rate(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return LearningService.rate(*args, **kwargs)


def advance(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return LearningService.advance(*args, **kwargs)


def pause(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return LearningService.pause(*args, **kwargs)


__all__ = [
    "LearningService",
    "LearningServiceError",
    "resolve_study_date",
    "get_settings",
    "update_settings",
    "save_scope",
    "list_chapters",
    "create_chapter",
    "update_chapter",
    "get_today",
    "open_today",
    "next_item",
    "override",
    "reveal",
    "rate",
    "advance",
    "pause",
]
