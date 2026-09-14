"""Recall-card content management service for Phase B.

This module owns chapter/card/revision persistence and the source-integrity
boundary.  It intentionally does not implement the daily study loop; the
learning service can consume these records after a revision is approved and
published.  All mutating operations use a short SQLite write transaction so
batch review and progress reset are atomic.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from app.database import db_session


class RecallCardsError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.detail = {"code": code, "message": message}
        super().__init__(message)


class SourceCitationError(RecallCardsError):
    pass


class ConflictError(RecallCardsError):
    pass


SETTINGS_ID = "singleton"
VISIBLE_STATUSES = {"published", "approved"}
CARD_LIFECYCLE = {"draft", "published", "paused", "archived", "rejected"}
CARD_STUDY_STATUSES = {"new", "learning", "review", "suspended", "archived"}
REVISION_STATUSES = {"draft", "approved", "rejected", "published"}


def _id() -> str:
    return str(uuid.uuid4())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _loads(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(value)
        return parsed if parsed is not None else default
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        return dict(value.model_dump(exclude_unset=True))
    if hasattr(value, "dict"):
        return dict(value.dict(exclude_unset=True))
    return {key: value for key, value in vars(value).items() if not key.startswith("_")}


def _first(data: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return default


def _clean_text(value: Any, field: str, *, required: bool = False, max_chars: Optional[int] = None) -> Optional[str]:
    if value is None:
        result = ""
    else:
        result = str(value).strip()
    if required and not result:
        raise RecallCardsError("INVALID_REQUEST", f"{field} 不能为空", 422)
    if max_chars is not None and len(result) > max_chars:
        raise RecallCardsError("INVALID_REQUEST", f"{field} 超过 {max_chars} 字符限制", 422)
    return result or None


def normalize_title(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def chunk_content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _as_utc_timestamp(value: Any) -> Any:
    """Expose stored UTC timestamps with an explicit ``+00:00`` offset."""
    if not isinstance(value, str) or not value:
        return value
    text = value.strip()
    if text.endswith("Z") or "+" in text[10:] or text[10:].count("-") > 0:
        return text
    try:
        parsed = datetime.fromisoformat(text.replace(" ", "T"))
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


def _row(row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
    return _normalise_timestamps(dict(row)) if row is not None else None


@contextmanager
def _tx(conn: sqlite3.Connection):
    savepoint = None
    began = False
    if conn.in_transaction:
        savepoint = f"recall_{uuid.uuid4().hex}"
        conn.execute(f"SAVEPOINT {savepoint}")
    else:
        conn.execute("BEGIN IMMEDIATE")
        began = True
    try:
        yield conn
        if savepoint:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        elif began:
            conn.commit()
    except Exception:
        if savepoint:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        elif began:
            conn.rollback()
        raise


def _conn_or_session(conn: Optional[sqlite3.Connection]):
    if conn is not None:
        @contextmanager
        def existing():
            yield conn
        return existing()
    return db_session()


def _effective_version(data: Mapping[str, Any], *names: str) -> Optional[int]:
    value = _first(data, *names, "expected_version", "version", "lock_version")
    if value is None:
        return None
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise RecallCardsError("INVALID_REQUEST", "version 必须是整数", 422) from exc
    if value < 1:
        raise RecallCardsError("INVALID_REQUEST", "version 必须为正整数", 422)
    return value


def _check_version(row: Mapping[str, Any], expected: Optional[int], *, field: str = "version") -> None:
    if expected is None:
        return
    actual = int(row.get("lock_version") or row.get("version") or 1)
    if expected != actual:
        raise ConflictError("VERSION_CONFLICT", f"{field} 已变化，请刷新后重试", 409)


def _citation_dict(value: Any) -> dict[str, Any]:
    data = _mapping(value)
    aliases = {
        "doc_name": "doc_name_snapshot", "document_name": "doc_name_snapshot", "filename": "doc_name_snapshot",
        "page": "page_number", "reference": "paragraph_ref", "quote": "snippet", "text": "snippet",
        "content_hash": "chunk_content_hash", "hash": "chunk_content_hash",
    }
    for old, new in aliases.items():
        if new not in data and old in data:
            data[new] = data[old]
    return data


def validate_source_citations(conn: sqlite3.Connection, citations: Sequence[Any], *, source_doc_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Validate and snapshot citations against the current chunk membership/text.

    A citation must identify a real chunk and carry all immutable snapshot fields.
    The supplied snippet may be a substring (whitespace-normalized matching is
    accepted), but the content hash must be exactly the current chunk hash.
    """
    if citations is None:
        return []
    if not isinstance(citations, (list, tuple)):
        raise SourceCitationError("INVALID_CITATIONS", "source_citations 必须是数组", 422)
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(citations, 1):
        data = _citation_dict(raw)
        doc_id = _clean_text(data.get("doc_id"), f"第 {index} 条引用 doc_id", required=True, max_chars=300)
        chunk_id = _clean_text(data.get("chunk_id"), f"第 {index} 条引用 chunk_id", required=True, max_chars=300)
        doc_name = _clean_text(data.get("doc_name_snapshot"), f"第 {index} 条引用 doc_name_snapshot", required=True, max_chars=1000)
        snippet = _clean_text(data.get("snippet"), f"第 {index} 条引用 snippet", required=True, max_chars=5000)
        content_hash = _clean_text(data.get("chunk_content_hash"), f"第 {index} 条引用 chunk_content_hash", required=True, max_chars=128)
        if chunk_id in seen:
            raise SourceCitationError("DUPLICATE_CITATION", f"第 {index} 条引用重复", 422)
        seen.add(chunk_id)
        chunk = conn.execute(
            """SELECT c.id, c.doc_id, c.page_number, c.paragraph_ref, c.content,
                      d.filename FROM document_chunks c JOIN documents d ON d.id = c.doc_id
               WHERE c.id = ?""", (chunk_id,)
        ).fetchone()
        if not chunk:
            raise SourceCitationError("SOURCE_CHUNK_NOT_FOUND", f"来源切片不存在: {chunk_id}", 422)
        if source_doc_id and chunk["doc_id"] != source_doc_id:
            raise SourceCitationError("SOURCE_DOC_MISMATCH", f"切片 {chunk_id} 不属于指定资料", 422)
        if str(doc_id) != str(chunk["doc_id"]):
            raise SourceCitationError("SOURCE_DOC_MISMATCH", f"切片 {chunk_id} 的 doc_id 不匹配", 422)
        current_content = str(chunk["content"] or "")
        current_hash = chunk_content_hash(current_content)
        if content_hash != current_hash:
            raise SourceCitationError("SOURCE_HASH_MISMATCH", f"切片 {chunk_id} 内容哈希不匹配", 422)
        if str(doc_name) != str(chunk["filename"]):
            raise SourceCitationError("SOURCE_DOC_NAME_MISMATCH", f"切片 {chunk_id} 文档名称不匹配", 422)
        normalized_snippet = " ".join(str(snippet).split())
        normalized_content = " ".join(current_content.split())
        if normalized_snippet not in normalized_content:
            raise SourceCitationError("SOURCE_SNIPPET_MISMATCH", f"切片 {chunk_id} 不包含引用摘录", 422)
        page = data.get("page_number")
        if page is not None:
            try:
                page = int(page)
            except (TypeError, ValueError) as exc:
                raise SourceCitationError("INVALID_CITATIONS", f"第 {index} 条引用 page_number 无效", 422) from exc
            if page < 1:
                raise SourceCitationError("INVALID_CITATIONS", f"第 {index} 条引用 page_number 无效", 422)
            if chunk["page_number"] is not None and page != chunk["page_number"]:
                raise SourceCitationError("SOURCE_LOCATION_MISMATCH", f"切片 {chunk_id} 页码不匹配", 422)
        else:
            page = chunk["page_number"]
        paragraph = data.get("paragraph_ref")
        if paragraph is not None and chunk["paragraph_ref"] is not None and str(paragraph) != str(chunk["paragraph_ref"]):
            raise SourceCitationError("SOURCE_LOCATION_MISMATCH", f"切片 {chunk_id} 段落引用不匹配", 422)
        result.append({
            "doc_id": str(chunk["doc_id"]),
            "chunk_id": str(chunk["id"]),
            "doc_name_snapshot": str(chunk["filename"]),
            "page_number": page,
            "paragraph_ref": chunk["paragraph_ref"] if paragraph is None else paragraph,
            "snippet": str(snippet),
            "chunk_content_hash": current_hash,
        })
    return result


def _content_values(data: Mapping[str, Any], *, require_content: bool = True) -> dict[str, Any]:
    prompt = _clean_text(_first(data, "prompt", "question", "front", "front_content"), "prompt", required=require_content, max_chars=10000)
    answer = _clean_text(_first(data, "answer", "back", "back_content"), "answer", required=require_content, max_chars=30000)
    hint = _clean_text(_first(data, "hint", "cue"), "hint", max_chars=5000)
    excerpt = _clean_text(_first(data, "source_excerpt", "excerpt"), "source_excerpt", max_chars=10000)
    tags = _first(data, "tags", default=[])
    if tags is None:
        tags = []
    if not isinstance(tags, (list, tuple)):
        raise RecallCardsError("INVALID_REQUEST", "tags 必须是数组", 422)
    tags = [str(tag).strip() for tag in tags if str(tag).strip()][:50]
    citations = _first(data, "source_citations", "source_refs", "citations", default=[])
    return {
        "prompt": prompt or "",
        "answer": answer or "",
        "hint": hint,
        "source_excerpt": excerpt,
        "tags": tags,
        "citations": citations,
        "concise_content": _clean_text(data.get("concise_content"), "concise_content", max_chars=30000),
        "detailed_content": _clean_text(data.get("detailed_content"), "detailed_content", max_chars=50000),
        "answer_structure": _clean_text(data.get("answer_structure"), "answer_structure", max_chars=30000),
        "confusing_points_and_cases": _clean_text(data.get("confusing_points_and_cases"), "confusing_points_and_cases", max_chars=30000),
        "note": _clean_text(data.get("note"), "note", max_chars=5000),
    }


def _citation_json(citations: Sequence[Mapping[str, Any]]) -> str:
    return _json(list(citations))


def _revision_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    row = _normalise_timestamps(dict(row)) or {}
    content = _loads(row.get("content_json"), {})
    if not isinstance(content, dict):
        content = {}
    tags = _loads(row.get("tags_json"), content.get("tags", []))
    if not isinstance(tags, list):
        tags = []
    citations = _loads(row.get("source_citations_json"), _loads(row.get("citations_json"), []))
    if not isinstance(citations, list):
        citations = []
    prompt = row.get("prompt") or row.get("question") or row.get("front_content") or content.get("prompt") or content.get("question") or ""
    answer = row.get("answer") or row.get("back_content") or content.get("answer") or ""
    return {
        "id": row.get("id"), "card_id": row.get("card_id"), "revision_number": int(row.get("revision_number") or row.get("revision_no") or 0),
        "status": row.get("status") or row.get("review_status") or "draft",
        "prompt": prompt, "question": prompt, "answer": answer, "back": answer,
        "hint": row.get("hint") or content.get("hint"), "source_excerpt": row.get("source_excerpt") or content.get("source_excerpt"),
        "tags": tags if isinstance(tags, list) else [], "source_citations": citations if isinstance(citations, list) else [], "source_refs": citations if isinstance(citations, list) else [],
        "concise_content": row.get("concise_content") or content.get("concise_content") or content.get("front_content"), "detailed_content": row.get("detailed_content") or content.get("detailed_content") or content.get("back_content"),
        "answer_structure": row.get("answer_structure") or content.get("answer_structure") or content.get("outline_points"), "confusing_points_and_cases": row.get("confusing_points_and_cases") or content.get("confusing_points_and_cases"),
        "outline_points": _loads(row.get("outline_points_json"), content.get("outline_points", [])), "explanation": row.get("explanation") or content.get("explanation"), "expanded_answer": row.get("expanded_answer") or content.get("expanded_answer"),
        "base_revision_id": row.get("base_revision_id"), "note": row.get("review_note") or content.get("note"), "rejection_reason": row.get("rejection_reason"),
        "created_by": row.get("created_by"), "reviewed_by": row.get("reviewed_by"), "created_at": str(row.get("created_at")), "updated_at": str(row.get("updated_at")), "published_at": row.get("published_at"),
    }


def _card_payload(conn: sqlite3.Connection, row: Mapping[str, Any], *, include_draft: bool = False) -> dict[str, Any]:
    card = _normalise_timestamps(dict(row)) or {}
    revision_id = card.get("published_revision_id") or card.get("current_revision_id")
    if include_draft and card.get("current_revision_id"):
        revision_id = card["current_revision_id"]
    revision = None
    if revision_id:
        revision = _row(conn.execute("SELECT * FROM recall_card_revisions WHERE id = ?", (revision_id,)).fetchone())
    if revision is None and card.get("published_revision_id"):
        revision = _row(conn.execute("SELECT * FROM recall_card_revisions WHERE id = ?", (card["published_revision_id"],)).fetchone())
    revision_payload = _revision_payload(revision) if revision else {}
    chapter = _row(conn.execute("SELECT title FROM recall_chapters WHERE id = ?", (card.get("chapter_id"),)).fetchone())
    progress = _row(conn.execute("SELECT * FROM learning_progress WHERE card_id = ? AND layer = 'outline' LIMIT 1", (card.get("id"),)).fetchone())
    tags = revision_payload.get("tags") or _loads(card.get("tags_json"), [])
    citations = revision_payload.get("source_citations") or _loads(card.get("citations_json"), []) or []
    lifecycle = card.get("publication_status") or card.get("status") or "draft"
    if int(card.get("is_archived") or 0): lifecycle = "archived"
    elif int(card.get("is_paused") or 0): lifecycle = "paused"
    return {
        "id": card.get("id"), "chapter_id": card.get("chapter_id"), "chapter_name": chapter.get("title") if chapter else None,
        "source_doc_id": card.get("source_doc_id"), "title": card.get("title") or revision_payload.get("prompt"),
        "prompt": revision_payload.get("prompt") or card.get("prompt") or "", "question": revision_payload.get("prompt") or card.get("prompt") or "",
        "answer": revision_payload.get("answer") or card.get("answer") or "", "back": revision_payload.get("answer") or card.get("answer") or "",
        "hint": revision_payload.get("hint") or card.get("hint"), "source_excerpt": revision_payload.get("source_excerpt") or card.get("source_excerpt"),
        "tags": tags if isinstance(tags, list) else [], "source_citations": citations, "source_refs": citations,
        "status": card.get("study_status") or card.get("status") if card.get("status") in CARD_STUDY_STATUSES else (progress.get("state") if progress else "new"),
        "lifecycle_status": lifecycle, "origin": card.get("origin") or "manual", "is_confirmed": bool(card.get("is_confirmed")), "is_ai_generated": bool(card.get("is_ai_generated")),
        "candidate": bool(card.get("candidate") or not card.get("is_confirmed")), "due_at": progress.get("due_date") if progress else None,
        "last_reviewed_at": progress.get("last_studied_at") if progress else None, "review_count": int(progress.get("review_count") or 0) if progress else 0,
        "interval_days": progress.get("interval_days") if progress else None, "ease_factor": progress.get("ease_factor") if progress else None,
        "revision_id": revision_payload.get("id"), "published_revision_id": card.get("published_revision_id"),
        "current_revision_no": int(card.get("current_revision_no") or revision_payload.get("revision_number") or 0), "version": int(card.get("lock_version") or card.get("version") or 1),
        "sort_order": int(card.get("sort_order") or 0), "created_at": str(card.get("created_at")), "updated_at": str(card.get("updated_at")),
        "published_at": card.get("published_at"), "archived_at": card.get("archived_at"),
        "concise_content": revision_payload.get("concise_content"), "detailed_content": revision_payload.get("detailed_content"),
        "answer_structure": revision_payload.get("answer_structure"), "confusing_points_and_cases": revision_payload.get("confusing_points_and_cases"),
        "outline_points": revision_payload.get("outline_points", []), "explanation": revision_payload.get("explanation"), "expanded_answer": revision_payload.get("expanded_answer"),
    }


class RecallCardsService:
    """CRUD, revision, review, publication, and lifecycle operations."""

    @staticmethod
    def list_chapters(*, source_doc_id: Optional[str] = None, include_archived: bool = False, conn: Optional[sqlite3.Connection] = None) -> list[dict[str, Any]]:
        with _conn_or_session(conn) as db:
            where, params = [], []
            if source_doc_id:
                where.append("source_doc_id = ?"); params.append(source_doc_id)
            if not include_archived:
                where.append("status <> 'archived'")
            query = "SELECT * FROM recall_chapters"
            if where: query += " WHERE " + " AND ".join(where)
            query += " ORDER BY sort_order ASC, title COLLATE NOCASE ASC, id ASC"
            rows = db.execute(query, params).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["name"] = item.get("title")
                item["enabled"] = bool(item.get("enabled", 1))
                item["card_count"] = db.execute("SELECT COUNT(*) FROM recall_cards WHERE chapter_id = ? AND is_archived = 0", (item["id"],)).fetchone()[0]
                item["due_count"] = 0
                item["metadata"] = _loads(item.get("metadata_json"), None)
                result.append(item)
            return result

    @staticmethod
    def get_chapter(chapter_id: str, *, conn: Optional[sqlite3.Connection] = None) -> dict[str, Any]:
        with _conn_or_session(conn) as db:
            row = db.execute("SELECT * FROM recall_chapters WHERE id = ?", (chapter_id,)).fetchone()
            if not row: raise RecallCardsError("CHAPTER_NOT_FOUND", "章节不存在", 404)
            return RecallCardsService.list_chapters(conn=db, include_archived=True)[next(i for i, item in enumerate(RecallCardsService.list_chapters(conn=db, include_archived=True)) if item["id"] == chapter_id)]

    @staticmethod
    def create_chapter(data: Any = None, *, conn: Optional[sqlite3.Connection] = None, **kwargs: Any) -> dict[str, Any]:
        values = _mapping(data); values.update(kwargs)
        title = _clean_text(_first(values, "title", "name"), "title", required=True, max_chars=500)
        normalized = normalize_title(title)
        source_doc_id = _clean_text(_first(values, "source_doc_id", "doc_id"), "source_doc_id", max_chars=300)
        chapter_id = str(_first(values, "id", "chapter_id", default=_id()))
        parent_id = _clean_text(values.get("parent_id"), "parent_id", max_chars=300)
        sort_order = int(_first(values, "sort_order", "position", default=0) or 0)
        enabled = 1 if bool(_first(values, "enabled", default=True)) else 0
        expanded = 1 if bool(_first(values, "expanded_enabled", default=False)) else 0
        with _conn_or_session(conn) as db, _tx(db):
            if source_doc_id:
                doc = db.execute("SELECT filename FROM documents WHERE id = ?", (source_doc_id,)).fetchone()
                if not doc: raise RecallCardsError("SOURCE_DOC_NOT_FOUND", "来源文档不存在", 422)
                doc_name = doc["filename"]
            else: doc_name = _clean_text(values.get("source_doc_name_snapshot"), "source_doc_name_snapshot", max_chars=1000)
            duplicate = db.execute("SELECT id FROM recall_chapters WHERE source_doc_id IS ? AND normalized_title = ?", (source_doc_id, normalized)).fetchone()
            if duplicate: raise ConflictError("CHAPTER_EXISTS", "同一资料下已存在同名章节", 409)
            try:
                db.execute("""INSERT INTO recall_chapters
                    (id, source_doc_id, source_doc_name_snapshot, title, normalized_title, title_normalized,
                     parent_id, status, enabled, expanded_enabled, version, sort_order, metadata_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, 1, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                    (chapter_id, source_doc_id, doc_name, title, normalized, normalized, parent_id, enabled, expanded, sort_order, _json(values.get("metadata") or {})))
            except sqlite3.IntegrityError as exc: raise ConflictError("CHAPTER_EXISTS", "同一资料下已存在同名章节", 409) from exc
            return RecallCardsService.get_chapter(chapter_id, conn=db)

    @staticmethod
    def update_chapter(chapter_id: str, data: Any = None, *, conn: Optional[sqlite3.Connection] = None, **kwargs: Any) -> dict[str, Any]:
        values = _mapping(data); values.update(kwargs)
        with _conn_or_session(conn) as db, _tx(db):
            current = _row(db.execute("SELECT * FROM recall_chapters WHERE id = ?", (chapter_id,)).fetchone())
            if not current: raise RecallCardsError("CHAPTER_NOT_FOUND", "章节不存在", 404)
            _check_version(current, _effective_version(values), field="chapter version")
            updates, params = [], []
            if "title" in values or "name" in values:
                title = _clean_text(_first(values, "title", "name"), "title", required=True, max_chars=500)
                normalized = normalize_title(title)
                duplicate = db.execute("SELECT id FROM recall_chapters WHERE source_doc_id IS ? AND normalized_title = ? AND id <> ?", (current.get("source_doc_id"), normalized, chapter_id)).fetchone()
                if duplicate: raise ConflictError("CHAPTER_EXISTS", "同一资料下已存在同名章节", 409)
                updates += ["title = ?", "normalized_title = ?", "title_normalized = ?"]; params += [title, normalized, normalized]
            for key, column in (("source_doc_id", "source_doc_id"), ("parent_id", "parent_id"), ("sort_order", "sort_order"), ("enabled", "enabled"), ("expanded_enabled", "expanded_enabled"), ("status", "status")):
                if key in values:
                    value = values[key]
                    if key == "status" and value not in {"active", "archived"}: raise RecallCardsError("INVALID_REQUEST", "status 无效", 422)
                    updates.append(f"{column} = ?"); params.append(value if key not in {"enabled", "expanded_enabled"} else (1 if value else 0))
            if "metadata" in values: updates.append("metadata_json = ?"); params.append(_json(values["metadata"] or {}))
            if updates:
                params.append(chapter_id); db.execute(f"UPDATE recall_chapters SET {', '.join(updates)}, version = COALESCE(version, 1) + 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?", params)
            return RecallCardsService.get_chapter(chapter_id, conn=db)

    @staticmethod
    def archive_chapter(chapter_id: str, *, expected_version: Optional[int] = None, conn: Optional[sqlite3.Connection] = None) -> dict[str, Any]:
        return RecallCardsService.update_chapter(chapter_id, {"status": "archived", "expected_version": expected_version}, conn=conn)

    @staticmethod
    def list_cards(*, chapter_id: Optional[str] = None, knowledge_point_id: Optional[str] = None, kp: Optional[str] = None, status: Optional[str] = None, search: Optional[str] = None, include_candidates: bool = False, limit: int = 100, offset: int = 0, conn: Optional[sqlite3.Connection] = None) -> list[dict[str, Any]]:
        with _conn_or_session(conn) as db:
            where, params = [], []
            if chapter_id: where.append("rc.chapter_id = ?"); params.append(chapter_id)
            knowledge_point_id = knowledge_point_id or kp
            if knowledge_point_id: where.append("rc.knowledge_point_id = ?"); params.append(knowledge_point_id)
            if status and status != "all":
                if status in CARD_STUDY_STATUSES: where.append("(rc.status = ? OR EXISTS (SELECT 1 FROM learning_progress lp WHERE lp.card_id = rc.id AND lp.layer = 'outline' AND lp.state = ?))"); params.extend([status, status])
                else: where.append("COALESCE(rc.publication_status, rc.status) = ?"); params.append(status)
            if not include_candidates: where.append("rc.is_confirmed = 1 AND rc.is_ai_generated = 0 AND COALESCE(rc.publication_status, rc.status) IN ('published','approved')")
            if search:
                pattern = f"%{search.strip()}%"; where.append("(rc.title LIKE ? OR rc.prompt LIKE ? OR rc.answer LIKE ? OR EXISTS (SELECT 1 FROM recall_card_revisions rr WHERE rr.id = rc.current_revision_id AND (rr.prompt LIKE ? OR rr.answer LIKE ?)))"); params.extend([pattern] * 5)
            query = "SELECT rc.* FROM recall_cards rc"
            if where: query += " WHERE " + " AND ".join(where)
            query += " ORDER BY rc.sort_order ASC, rc.created_at DESC, rc.id ASC LIMIT ? OFFSET ?"; params += [min(max(int(limit), 1), 500), max(int(offset), 0)]
            return [_card_payload(db, row, include_draft=include_candidates) for row in db.execute(query, params).fetchall()]

    @staticmethod
    def get_card(card_id: str, *, include_candidate: bool = True, conn: Optional[sqlite3.Connection] = None) -> dict[str, Any]:
        with _conn_or_session(conn) as db:
            row = db.execute("SELECT * FROM recall_cards WHERE id = ?", (card_id,)).fetchone()
            if not row: raise RecallCardsError("CARD_NOT_FOUND", "回忆卡不存在", 404)
            if not include_candidate and (not row["is_confirmed"] or row["is_ai_generated"]): raise RecallCardsError("CARD_NOT_PUBLISHED", "回忆卡尚未发布", 404)
            return _card_payload(db, row, include_draft=include_candidate)

    @staticmethod
    def create_card(data: Any = None, *, conn: Optional[sqlite3.Connection] = None, **kwargs: Any) -> dict[str, Any]:
        values = _mapping(data); values.update(kwargs)
        chapter_id = _clean_text(values.get("chapter_id"), "chapter_id", required=True, max_chars=300)
        content = _content_values(values)
        origin = str(_first(values, "origin", default="manual")); lifecycle = str(_first(values, "lifecycle_status", "publication_status", default="draft")); study_status = str(_first(values, "status", default="new"))
        if origin not in {"manual", "generated", "ai_generated", "imported"}: raise RecallCardsError("INVALID_REQUEST", "origin 无效", 422)
        if lifecycle not in CARD_LIFECYCLE: raise RecallCardsError("INVALID_REQUEST", "lifecycle_status 无效", 422)
        if study_status not in CARD_STUDY_STATUSES: raise RecallCardsError("INVALID_REQUEST", "status 无效", 422)
        source_doc_id = _clean_text(values.get("source_doc_id"), "source_doc_id", max_chars=300)
        with _conn_or_session(conn) as db, _tx(db):
            if not db.execute("SELECT 1 FROM recall_chapters WHERE id = ?", (chapter_id,)).fetchone(): raise RecallCardsError("CHAPTER_NOT_FOUND", "章节不存在", 404)
            citations = validate_source_citations(db, content["citations"], source_doc_id=source_doc_id)
            card_id, revision_id = str(_first(values, "id", "card_id", default=_id())), _id()
            revision_no = 1
            db.execute("""INSERT INTO recall_cards
                (id, chapter_id, source_doc_id, generation_batch_id, knowledge_point_id, title, prompt, answer, hint, source_excerpt, tags_json,
                 card_type, origin, status, publication_status, is_confirmed, is_ai_generated, candidate, is_paused, is_archived, sort_order,
                 current_revision_id, current_revision_no, version, lock_version, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                (card_id, chapter_id, source_doc_id, values.get("generation_batch_id"), values.get("knowledge_point_id"), values.get("title") or content["prompt"], content["prompt"], content["answer"], content["hint"], content["source_excerpt"], _json(content["tags"]), values.get("card_type", "basic"), origin, study_status, lifecycle, 1 if lifecycle in {"published", "approved"} else 0, 1 if origin in {"generated", "ai_generated"} else 0, 0 if lifecycle in {"published", "approved"} else 1, 0, 0, int(values.get("sort_order", 0) or 0), revision_id, revision_no, 1, 1, _json(values.get("metadata") or {})))
            db.execute("""INSERT INTO recall_card_revisions
                (id, card_id, revision_number, revision_no, status, review_status, content_json, front_content, back_content, prompt, hint, source_excerpt, tags_json,
                 question, answer, concise_content, detailed_content, answer_structure, confusing_points_and_cases, source_citations_json, created_by, created_at, updated_at)
                VALUES (?, ?, 1, 1, 'draft', 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                (revision_id, card_id, _json({**content, "citations": citations}), content["prompt"], content["answer"], content["prompt"], content["hint"], content["source_excerpt"], _json(content["tags"]), content["prompt"], content["answer"], content["concise_content"], content["detailed_content"], content["answer_structure"], content["confusing_points_and_cases"], _citation_json(citations), values.get("created_by", "user")))
            db.execute("INSERT OR IGNORE INTO learning_progress (id, card_id, chapter_id, layer, content_revision_no, state, interval_index, due_date, state_version) VALUES (?, ?, ?, 'outline', 1, 'new', 0, NULL, 1)", (_id(), card_id, chapter_id))
            _log_event(db, "card_created", "card", card_id, chapter_id=chapter_id, data={"revision_id": revision_id})
            return RecallCardsService.get_card(card_id, conn=db)

    @staticmethod
    def update_card(card_id: str, data: Any = None, *, conn: Optional[sqlite3.Connection] = None, **kwargs: Any) -> dict[str, Any]:
        values = _mapping(data); values.update(kwargs)
        with _conn_or_session(conn) as db, _tx(db):
            card = _row(db.execute("SELECT * FROM recall_cards WHERE id = ?", (card_id,)).fetchone())
            if not card: raise RecallCardsError("CARD_NOT_FOUND", "回忆卡不存在", 404)
            _check_version(card, _effective_version(values), field="card version")
            updates, params = [], []
            source_doc_id = values.get("source_doc_id", card.get("source_doc_id"))
            content_keys = {"prompt", "question", "front", "answer", "back", "hint", "source_excerpt", "tags", "source_citations", "source_refs", "citations", "concise_content", "detailed_content", "answer_structure", "confusing_points_and_cases"}
            if content_keys.intersection(values):
                existing_revision = _row(db.execute("SELECT * FROM recall_card_revisions WHERE id = ?", (card.get("current_revision_id"),)).fetchone()) or {}
                merged = _content_values({**_revision_payload(existing_revision), **values}, require_content=True)
                citations = validate_source_citations(db, merged["citations"], source_doc_id=source_doc_id)
                revision = RecallCardsService.create_revision(card_id, {**merged, "source_citations": citations, "base_revision_id": card.get("current_revision_id")}, conn=db)
                updates += ["current_revision_id = ?", "current_revision_no = ?"]; params += [revision["id"], revision["revision_number"]]
            for key, column in (("chapter_id", "chapter_id"), ("title", "title"), ("source_doc_id", "source_doc_id"), ("sort_order", "sort_order"), ("metadata", "metadata_json")):
                if key in values:
                    updates.append(f"{column} = ?"); params.append(_json(values[key] or {}) if key == "metadata" else values[key])
            if "status" in values:
                status = str(values["status"]); 
                if status not in CARD_STUDY_STATUSES: raise RecallCardsError("INVALID_REQUEST", "status 无效", 422)
                updates.append("status = ?"); params.append(status)
            if updates:
                params.append(card_id); db.execute(f"UPDATE recall_cards SET {', '.join(updates)}, version = COALESCE(version,1)+1, lock_version = COALESCE(lock_version,1)+1, updated_at = CURRENT_TIMESTAMP WHERE id = ?", params)
            _log_event(db, "card_updated", "card", card_id, chapter_id=card.get("chapter_id"), data={"fields": list(values)})
            return RecallCardsService.get_card(card_id, conn=db)

    @staticmethod
    def create_revision(card_id: str, data: Any = None, *, conn: Optional[sqlite3.Connection] = None, **kwargs: Any) -> dict[str, Any]:
        values = _mapping(data); values.update(kwargs)
        with _conn_or_session(conn) as db, _tx(db):
            card = _row(db.execute("SELECT * FROM recall_cards WHERE id = ?", (card_id,)).fetchone())
            if not card: raise RecallCardsError("CARD_NOT_FOUND", "回忆卡不存在", 404)
            expected = _effective_version(values, "expected_card_version", "card_version")
            _check_version(card, expected, field="card version")
            content = _content_values(values)
            citations = validate_source_citations(db, content["citations"], source_doc_id=card.get("source_doc_id"))
            max_no = db.execute("SELECT COALESCE(MAX(revision_number), 0) FROM recall_card_revisions WHERE card_id = ?", (card_id,)).fetchone()[0]
            revision_id = _id(); no = int(max_no) + 1
            db.execute("""INSERT INTO recall_card_revisions
                (id, card_id, revision_number, revision_no, status, review_status, content_json, front_content, back_content, prompt, hint, source_excerpt, tags_json,
                 question, answer, concise_content, detailed_content, answer_structure, confusing_points_and_cases, source_citations_json, base_revision_id, created_by, review_note, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'draft', 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                (revision_id, card_id, no, no, _json({**content, "citations": citations}), content["prompt"], content["answer"], content["prompt"], content["hint"], content["source_excerpt"], _json(content["tags"]), content["prompt"], content["answer"], content["concise_content"], content["detailed_content"], content["answer_structure"], content["confusing_points_and_cases"], _citation_json(citations), values.get("base_revision_id") or card.get("current_revision_id"), values.get("created_by", "user"), content["note"]))
            db.execute("UPDATE recall_cards SET current_revision_id = ?, current_revision_no = ?, version = COALESCE(version,1)+1, lock_version = COALESCE(lock_version,1)+1, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (revision_id, no, card_id))
            _log_event(db, "revision_created", "revision", revision_id, card_id=card_id, chapter_id=card.get("chapter_id"), data={"revision_number": no})
            return _revision_payload(_row(db.execute("SELECT * FROM recall_card_revisions WHERE id = ?", (revision_id,)).fetchone()))

    @staticmethod
    def update_revision(revision_id: str, data: Any = None, *, conn: Optional[sqlite3.Connection] = None, **kwargs: Any) -> dict[str, Any]:
        values = _mapping(data); values.update(kwargs)
        with _conn_or_session(conn) as db, _tx(db):
            revision = _row(db.execute("SELECT * FROM recall_card_revisions WHERE id = ?", (revision_id,)).fetchone())
            if not revision: raise RecallCardsError("REVISION_NOT_FOUND", "版本不存在", 404)
            if revision.get("status") not in {"draft", "rejected"}: raise ConflictError("REVISION_LOCKED", "已审核版本不可编辑", 409)
            expected = _effective_version(values, "expected_version")
            if expected is not None:
                actual = int(revision.get("version") or revision.get("updated_version") or 1)
                if expected != actual: raise ConflictError("VERSION_CONFLICT", "版本已变化，请刷新后重试", 409)
            merged = {**_revision_payload(revision), **values}
            content = _content_values(merged)
            card = _row(db.execute("SELECT * FROM recall_cards WHERE id = ?", (revision["card_id"],)).fetchone())
            citations = validate_source_citations(db, content["citations"], source_doc_id=card.get("source_doc_id"))
            db.execute("""UPDATE recall_card_revisions SET content_json = ?, front_content = ?, back_content = ?, prompt = ?, hint = ?, source_excerpt = ?, tags_json = ?, question = ?, answer = ?, concise_content = ?, detailed_content = ?, answer_structure = ?, confusing_points_and_cases = ?, source_citations_json = ?, review_note = ?, status = 'draft', review_status = 'draft', updated_at = CURRENT_TIMESTAMP WHERE id = ?""", (_json({**content, "citations": citations}), content["prompt"], content["answer"], content["prompt"], content["hint"], content["source_excerpt"], _json(content["tags"]), content["prompt"], content["answer"], content["concise_content"], content["detailed_content"], content["answer_structure"], content["confusing_points_and_cases"], _citation_json(citations), content["note"], revision_id))
            return _revision_payload(_row(db.execute("SELECT * FROM recall_card_revisions WHERE id = ?", (revision_id,)).fetchone()))

    @staticmethod
    def review_revisions(data: Any = None, *, conn: Optional[sqlite3.Connection] = None, **kwargs: Any) -> Any:
        values = _mapping(data); values.update(kwargs)
        raw_items = _first(values, "revisions", "items", "decisions", default=None)
        if raw_items is None and values.get("revision_id"): raw_items = [values]
        if not isinstance(raw_items, (list, tuple)) or not raw_items: raise RecallCardsError("INVALID_REQUEST", "至少需要一条版本审核决定", 422)
        normalized = []
        for raw in raw_items:
            item = _mapping(raw); decision = str(_first(item, "decision", "action", default=""));
            if decision in {"approve", "approved", "publish"}: decision = "approve"
            elif decision in {"reject", "rejected", "ignore"}: decision = "reject"
            else: raise RecallCardsError("INVALID_REQUEST", "decision 必须是 approve 或 reject", 422)
            rid = _clean_text(item.get("revision_id"), "revision_id", required=True, max_chars=300)
            normalized.append((rid, decision, item.get("note")))
        with _conn_or_session(conn) as db, _tx(db):
            # Validate every row before making any update: all-or-nothing batch.
            rows = []
            for rid, decision, note in normalized:
                row = _row(db.execute("SELECT * FROM recall_card_revisions WHERE id = ?", (rid,)).fetchone())
                if not row: raise RecallCardsError("REVISION_NOT_FOUND", f"版本不存在: {rid}", 404)
                if row.get("status") not in {"draft", "rejected"}: raise ConflictError("REVISION_LOCKED", f"版本不可审核: {rid}", 409)
                card = _row(db.execute("SELECT * FROM recall_cards WHERE id = ?", (row["card_id"],)).fetchone())
                expected = _effective_version(next(item for item in (_mapping(x) for x in raw_items) if str(item.get("revision_id")) == rid), "expected_card_version", "card_version")
                _check_version(card, expected, field="card version")
                rows.append((row, card, decision, note))
            result = []
            for row, card, decision, note in rows:
                if decision == "approve":
                    db.execute("UPDATE recall_card_revisions SET status = 'approved', review_status = 'approved', reviewed_by = ?, review_note = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (values.get("reviewed_by", "user"), note, row["id"]))
                    db.execute("UPDATE recall_cards SET candidate = 0, is_confirmed = 1, is_ai_generated = 0, publication_status = 'approved', status = 'draft', version = COALESCE(version,1)+1, lock_version = COALESCE(lock_version,1)+1, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (card["id"],))
                    event = "revision_approved"
                else:
                    db.execute("UPDATE recall_card_revisions SET status = 'rejected', review_status = 'rejected', reviewed_by = ?, review_note = ?, rejection_reason = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (values.get("reviewed_by", "user"), note, note, row["id"]))
                    db.execute("UPDATE recall_cards SET publication_status = 'rejected', status = 'draft', version = COALESCE(version,1)+1, lock_version = COALESCE(lock_version,1)+1, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (card["id"],))
                    event = "revision_rejected"
                _log_event(db, event, "revision", row["id"], card_id=card["id"], chapter_id=card.get("chapter_id"), data={"note": note})
                result.append(_revision_payload(_row(db.execute("SELECT * FROM recall_card_revisions WHERE id = ?", (row["id"],)).fetchone())))
            return result[0] if len(result) == 1 else result

    @staticmethod
    def publish_card(card_id: str, *, revision_id: Optional[str] = None, reset_progress: bool = False, expected_version: Optional[int] = None, conn: Optional[sqlite3.Connection] = None) -> dict[str, Any]:
        with _conn_or_session(conn) as db, _tx(db):
            card = _row(db.execute("SELECT * FROM recall_cards WHERE id = ?", (card_id,)).fetchone())
            if not card: raise RecallCardsError("CARD_NOT_FOUND", "回忆卡不存在", 404)
            _check_version(card, expected_version, field="card version")
            rid = revision_id or card.get("current_revision_id")
            revision = _row(db.execute("SELECT * FROM recall_card_revisions WHERE id = ? AND card_id = ?", (rid, card_id)).fetchone())
            if not revision: raise RecallCardsError("REVISION_NOT_FOUND", "待发布版本不存在", 404)
            if revision.get("status") not in {"approved", "published"}: raise ConflictError("REVISION_NOT_APPROVED", "只能发布已审核版本", 409)
            old_published = card.get("published_revision_id")
            db.execute("UPDATE recall_card_revisions SET status = 'published', published_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (rid,))
            db.execute("UPDATE recall_cards SET published_revision_id = ?, current_revision_id = ?, current_revision_no = ?, publication_status = 'published', status = 'published', is_confirmed = 1, is_ai_generated = 0, candidate = 0, version = COALESCE(version,1)+1, lock_version = COALESCE(lock_version,1)+1, published_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (rid, rid, revision.get("revision_number") or revision.get("revision_no"), card_id))
            if reset_progress and old_published and old_published != rid:
                # A reset must clear the whole schedule, not just the revision
                # pointer: intervals, counts, due dates and the reset marker are
                # all part of "start this card over".
                db.execute(
                    """
                    UPDATE learning_progress
                    SET content_revision_no = ?, state = 'new', status = 'new',
                        interval_index = 0, repetitions = 0, interval_days = 0,
                        ease_factor = 2.5, due_date = NULL, due_at = NULL,
                        first_study_date = NULL, last_studied_at = NULL,
                        last_reviewed_at = NULL, review_count = 0, reset_at = ?,
                        state_version = COALESCE(state_version, 1) + 1,
                        version = COALESCE(version, 1) + 1,
                        updated_at = ?
                    WHERE card_id = ?
                    """,
                    (revision.get("revision_number") or revision.get("revision_no"), _now(), _now(), card_id),
                )
                db.execute(
                    "UPDATE learning_day_units SET invalid_reason = 'CONTENT_CHANGED', updated_at = ? WHERE card_id = ? AND COALESCE(invalid_reason, '') = ''",
                    (_now(), card_id),
                )
            _log_event(db, "card_published", "card", card_id, chapter_id=card.get("chapter_id"), data={"revision_id": rid, "reset_progress": reset_progress})
            return RecallCardsService.get_card(card_id, conn=db)

    @staticmethod
    def set_card_lifecycle(card_id: str, status: str, *, expected_version: Optional[int] = None, conn: Optional[sqlite3.Connection] = None) -> dict[str, Any]:
        status = str(status)
        if status not in CARD_LIFECYCLE: raise RecallCardsError("INVALID_REQUEST", "卡片生命周期状态无效", 422)
        with _conn_or_session(conn) as db, _tx(db):
            card = _row(db.execute("SELECT * FROM recall_cards WHERE id = ?", (card_id,)).fetchone())
            if not card: raise RecallCardsError("CARD_NOT_FOUND", "回忆卡不存在", 404)
            _check_version(card, expected_version, field="card version")
            paused, archived = (1 if status == "paused" else 0), (1 if status == "archived" else 0)
            db.execute("UPDATE recall_cards SET publication_status = ?, status = CASE WHEN ? IN ('published','approved') THEN 'published' ELSE status END, is_paused = ?, is_archived = ?, archived_at = CASE WHEN ? = 'archived' THEN CURRENT_TIMESTAMP ELSE NULL END, version = COALESCE(version,1)+1, lock_version = COALESCE(lock_version,1)+1, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (status, status, paused, archived, status, card_id))
            _log_event(db, f"card_{status}", "card", card_id, chapter_id=card.get("chapter_id"), data={"status": status})
            return RecallCardsService.get_card(card_id, conn=db)

    @staticmethod
    def history(card_id: str, *, conn: Optional[sqlite3.Connection] = None) -> list[dict[str, Any]]:
        with _conn_or_session(conn) as db:
            revisions = [_revision_payload(row) for row in db.execute("SELECT * FROM recall_card_revisions WHERE card_id = ? ORDER BY revision_number DESC, created_at DESC", (card_id,)).fetchall()]
            events = []
            for row in db.execute("SELECT * FROM learning_events WHERE card_id = ? ORDER BY id DESC", (card_id,)).fetchall():
                event = dict(row); event["data"] = _loads(event.get("data_json"), None); event["action"] = event.get("event_type"); event["revision_id"] = (event["data"] or {}).get("revision_id")
                events.append(event)
            return revisions + events

    @staticmethod
    def list_generations(*, chapter_id: Optional[str] = None, limit: int = 100, offset: int = 0, conn: Optional[sqlite3.Connection] = None) -> list[dict[str, Any]]:
        with _conn_or_session(conn) as db:
            query = "SELECT * FROM recall_generation_batches"; params: list[Any] = []
            if chapter_id: query += " WHERE chapter_id = ?"; params.append(chapter_id)
            query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"; params += [min(max(int(limit), 1), 500), max(int(offset), 0)]
            result = []
            for row in db.execute(query, params).fetchall():
                item = dict(row); metadata = _loads(item.get("metadata_json"), {})
                item["request_id"] = item.get("request_id") or metadata.get("request_id"); item["request_fingerprint"] = item.get("request_fingerprint") or metadata.get("batch_fingerprint"); item["chapter_ids"] = [item["chapter_id"]] if item.get("chapter_id") else []
                item["card_count"] = item.get("candidate_count") or 0; item["created_count"] = item["candidate_count"] or 0; item["error_msg"] = item.get("error_message")
                result.append(item)
            return result


def _log_event(db: sqlite3.Connection, event_type: str, entity_type: str, entity_id: str, *, chapter_id: Optional[str] = None, card_id: Optional[str] = None, data: Optional[Mapping[str, Any]] = None) -> None:
    db.execute("INSERT INTO learning_events (event_type, entity_type, entity_id, chapter_id, card_id, data_json) VALUES (?, ?, ?, ?, ?, ?)", (event_type, entity_type, entity_id, chapter_id, card_id or (entity_id if entity_type == "card" else None), _json(data or {})))


# Module-level aliases for routers and integrations.
list_chapters = RecallCardsService.list_chapters
get_chapter = RecallCardsService.get_chapter
create_chapter = RecallCardsService.create_chapter
update_chapter = RecallCardsService.update_chapter
archive_chapter = RecallCardsService.archive_chapter
list_cards = RecallCardsService.list_cards
get_card = RecallCardsService.get_card
create_card = RecallCardsService.create_card
update_card = RecallCardsService.update_card
create_revision = RecallCardsService.create_revision
update_revision = RecallCardsService.update_revision
review_revisions = RecallCardsService.review_revisions
publish_card = RecallCardsService.publish_card
set_card_lifecycle = RecallCardsService.set_card_lifecycle
history = RecallCardsService.history
list_generations = RecallCardsService.list_generations

__all__ = [
    "RecallCardsError", "SourceCitationError", "ConflictError", "RecallCardsService",
    "normalize_title", "chunk_content_hash", "validate_source_citations",
    "list_chapters", "get_chapter", "create_chapter", "update_chapter", "archive_chapter",
    "list_cards", "get_card", "create_card", "update_card", "create_revision", "update_revision",
    "review_revisions", "publish_card", "set_card_lifecycle", "history", "list_generations",
]
