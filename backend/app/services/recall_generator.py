"""Grounded recall-card generation for the Phase E learning flow.

This module intentionally keeps generation independent from the HTTP/router layer.
A generation *batch* stores an immutable source snapshot, and the worker processes
one knowledge point at a time.  That gives us a useful retry boundary: a completed
knowledge point can be skipped without ever reusing mutable document content.

The v6 database is deliberately additive and stores the request/fingerprint and
snapshot in ``recall_generation_batches.metadata_json``.  Generated cards are
inserted as draft, unconfirmed candidates; only the review flow may publish them.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from fastapi import HTTPException

from app.config import settings
from app.database import db_session
from app.services.llm.client import LLMClient
from app.services.llm.prompts import RECALL_CARD_GENERATION_SYSTEM_PROMPT
from app.services.task_worker import TaskWorker

logger = logging.getLogger(__name__)

# Input/output limits are intentionally explicit and exported for callers/tests.
MIN_UNITS = 1
MAX_UNITS = 20
MIN_CHUNKS_PER_UNIT = 1
MAX_CHUNKS_PER_UNIT = 10
MAX_UNIT_CHARS = 12_000
MAX_CARDS_PER_UNIT = 12
MAX_CITATIONS_PER_CARD = 10
MAX_CARD_TITLE_CHARS = 200
MAX_CARD_FRONT_CHARS = 1_000
MAX_CARD_BACK_CHARS = 4_000
MAX_CARD_TYPE_CHARS = 40
MAX_QUOTE_CHARS = 2_000
MAX_INSUFFICIENT_REASON_CHARS = 500
MAX_REQUEST_ID_CHARS = 200

TASK_TYPE = "generate_recall_cards"


class RecallGenerationError(ValueError):
    """A deterministic validation or source-integrity failure."""


class SourceChangedError(RecallGenerationError):
    """The immutable input snapshot no longer matches persisted source text."""


@dataclass(frozen=True)
class SnapshotChunk:
    id: str
    doc_id: str
    doc_name: str
    source_hash: str
    content_hash: str
    content: str
    page_number: Optional[int] = None
    paragraph_ref: Optional[str] = None
    heading: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "chunk_id": self.id,
            "doc_id": self.doc_id,
            "doc_name": self.doc_name,
            "source_hash": self.source_hash,
            "document_hash": self.source_hash,
            "content_hash": self.content_hash,
            "page_number": self.page_number,
            "paragraph_ref": self.paragraph_ref,
            "heading": self.heading,
            "content": self.content,
        }


@dataclass(frozen=True)
class SnapshotUnit:
    knowledge_point_id: str
    knowledge_point_name: str
    knowledge_point_chapter: Optional[str]
    chapter_id: str
    chapter_title: str
    source_doc_id: Optional[str]
    chunks: Tuple[SnapshotChunk, ...]
    library_chapter_id: Optional[str] = None
    library_scope_fingerprint: Optional[str] = None

    @property
    def source_hashes(self) -> Dict[str, str]:
        return {chunk.id: chunk.source_hash for chunk in self.chunks}

    @property
    def content_chars(self) -> int:
        return sum(len(chunk.content) for chunk in self.chunks)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "knowledge_point_id": self.knowledge_point_id,
            "knowledge_point_name": self.knowledge_point_name,
            "knowledge_point_chapter": self.knowledge_point_chapter,
            "chapter_id": self.chapter_id,
            "chapter_title": self.chapter_title,
            "source_doc_id": self.source_doc_id,
            "source_hashes": self.source_hashes,
            "chunks": [chunk.as_dict() for chunk in self.chunks],
            "library_chapter_id": self.library_chapter_id,
            "library_scope_fingerprint": self.library_scope_fingerprint,
        }


# ---------------------------------------------------------------------------
# Small normalization/hash helpers
# ---------------------------------------------------------------------------


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        try:
            return dict(value.model_dump())
        except Exception:
            pass
    if hasattr(value, "dict"):
        try:
            return dict(value.dict())
        except Exception:
            pass
    try:
        return dict(value)
    except Exception as exc:  # pragma: no cover - defensive API boundary
        raise RecallGenerationError("知识点单元必须是对象") from exc


def _first(mapping: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def _text(value: Any, *, field: str, required: bool = False, max_chars: Optional[int] = None) -> str:
    if value is None:
        result = ""
    elif isinstance(value, str):
        result = value.strip()
    else:
        result = str(value).strip()
    if required and not result:
        raise RecallGenerationError(f"{field}不能为空")
    if max_chars is not None and len(result) > max_chars:
        raise RecallGenerationError(f"{field}超过 {max_chars} 字符限制")
    return result


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(snapshot_units: Sequence[SnapshotUnit], chapter_id: str, source_doc_id: Optional[str], instructions: str = "") -> str:
    payload = {
        "chapter_id": chapter_id,
        "source_doc_id": source_doc_id,
        "units": [unit.as_dict() for unit in snapshot_units],
        "instructions": instructions,
    }
    return _sha256_text(_canonical_json(payload))


def _normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _quote_matches(quote: str, content: str) -> bool:
    """Check an emitted quote without accepting invented text.

    Parsers sometimes preserve line breaks while a model emits spaces.  Exact
    substring matching is preferred; a whitespace-normalized substring is the
    only tolerated alternate representation.
    """
    if quote in content:
        return True
    return _normalize_whitespace(quote) in _normalize_whitespace(content)


def _demo_enabled() -> bool:
    return bool(getattr(settings, "DEMO_MODE", False)) or os.getenv("DEMO_MODE", "").lower() in {
        "1",
        "true",
        "yes",
    }


def _normalise_mode(mode: Optional[str]) -> str:
    value = (mode or "formal").strip().lower()
    if value in {"demo", "demonstration", "offline"}:
        return "demo"
    return "formal"


def _normalize_id(value: Any, *, field: str) -> str:
    result = _text(value, field=field, required=True, max_chars=300)
    if any(ch in result for ch in "\r\n\t"):
        raise RecallGenerationError(f"{field}包含非法空白字符")
    return result


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    """Read a sqlite row/mapping key without assuming one schema revision."""
    try:
        if isinstance(row, Mapping):
            return row.get(key, default)
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _table_columns(conn: Any, table: str) -> set[str]:
    """Return columns for additive-schema compatibility.

    Phase E was delivered while existing local databases can still be on an
    earlier v6 shape.  SQL writes therefore use only columns present in the
    active database; the immutable snapshot in metadata remains the canonical
    representation in every shape.
    """
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _insert_available(conn: Any, table: str, values: Mapping[str, Any]) -> None:
    available = _table_columns(conn, table)
    selected = [(key, value) for key, value in values.items() if not available or key in available]
    if not selected:
        raise RecallGenerationError(f"无法向 {table} 写入记录")
    columns = ", ".join(key for key, _ in selected)
    placeholders = ", ".join("?" for _ in selected)
    conn.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
        tuple(value for _, value in selected),
    )


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


class RecallGenerator:
    """Create immutable generation batches and execute them through TaskWorker."""

    # Public aliases make the service usable by both the initial learning API and
    # callers that use the older ``generate_*`` naming convention.

    @classmethod
    def create_batch(cls, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return cls.create_generation_batch(*args, **kwargs)

    @classmethod
    def enqueue(cls, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return cls.create_generation_batch(*args, **kwargs)

    @classmethod
    def create_generation(cls, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return cls.create_generation_batch(*args, **kwargs)

    @classmethod
    def create_generation_batch(
        cls,
        chapter_id: Optional[str] = None,
        units: Optional[Sequence[Any]] = None,
        *,
        knowledge_point_ids: Optional[Sequence[str]] = None,
        request_id: Optional[str] = None,
        mode: str = "formal",
        source_doc_id: Optional[str] = None,
        parent_task_id: Optional[str] = None,
        conn: Optional[Any] = None,
        message: Optional[str] = None,
        instructions: Optional[str] = None,
        library_chapter_id: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Validate a request, snapshot inputs, and atomically create batch/task.

        ``units`` is preferred because it makes the input snapshot explicit.  For
        compatibility, callers may pass ``knowledge_point_ids`` and the service
        derives source chunks from the chapter's source document.
        """
        # Accept common request-object forms without coupling to Pydantic models.
        request_obj = kwargs.pop("request", None)
        if request_obj is not None:
            request_map = _as_dict(request_obj)
            if chapter_id is None:
                chapter_id = _first(request_map, "chapter_id", "chapterId")
            if units is None:
                units = _first(request_map, "units", "knowledge_points", "knowledge_point_units")
            if knowledge_point_ids is None:
                knowledge_point_ids = _first(request_map, "knowledge_point_ids", "knowledgePointIds")
            if request_id is None:
                request_id = _first(request_map, "request_id", "requestId")
            mode = _first(request_map, "mode", "generation_mode", default=mode)
            if instructions is None:
                instructions = request_map.get("instructions")

        chapter_id = _normalize_id(chapter_id, field="chapter_id")
        request_id = _normalize_id(request_id or f"recall-{uuid.uuid4().hex}", field="request_id")
        if len(request_id) > MAX_REQUEST_ID_CHARS:
            raise RecallGenerationError(f"request_id超过 {MAX_REQUEST_ID_CHARS} 字符限制")
        mode = _normalise_mode(mode)
        instructions = _text(instructions, field="生成要求", max_chars=4000)

        def _create(active_conn: Any) -> Dict[str, Any]:
            chapter = active_conn.execute(
                "SELECT id, source_doc_id, title, normalized_title, status, version "
                "FROM recall_chapters WHERE id = ?",
                (chapter_id,),
            ).fetchone()
            if not chapter:
                raise RecallGenerationError("章节不存在")
            if chapter["status"] not in (None, "active"):
                raise RecallGenerationError("章节不是可生成状态")

            chapter_source_doc_id = chapter["source_doc_id"] or source_doc_id
            if source_doc_id and chapter["source_doc_id"] and source_doc_id != chapter["source_doc_id"]:
                raise RecallGenerationError("章节与资料归属不一致")

            # A request id is an idempotency key.  Replaying exactly the same
            # request returns its existing records; reusing it for another input is
            # rejected rather than silently mixing snapshots.
            existing = active_conn.execute(
                "SELECT * FROM recall_generation_batches WHERE chapter_id = ? "
                "ORDER BY created_at DESC",
                (chapter_id,),
            ).fetchall()
            for row in existing:
                metadata = cls._decode_json(row["metadata_json"], {})
                if metadata.get("request_id") == request_id:
                    existing_fp = metadata.get("batch_fingerprint")
                    if units is None and knowledge_point_ids is None:
                        if instructions != metadata.get("instructions", "") or library_chapter_id != metadata.get("library_chapter_id"):
                            raise RecallGenerationError("request_id已用于不同的批次输入")
                        return cls._batch_response(row, metadata)
                    candidate_units = cls._build_units(
                        active_conn,
                        chapter=dict(chapter),
                        units=units,
                        knowledge_point_ids=knowledge_point_ids,
                        chapter_source_doc_id=chapter_source_doc_id,
                        library_chapter_id=library_chapter_id,
                    )
                    candidate_fp = _fingerprint(candidate_units, chapter_id, chapter_source_doc_id, instructions)
                    if existing_fp != candidate_fp:
                        raise RecallGenerationError("request_id已用于不同的批次输入")
                    return cls._batch_response(row, metadata)

            snapshot_units = cls._build_units(
                active_conn,
                chapter=dict(chapter),
                units=units,
                knowledge_point_ids=knowledge_point_ids,
                chapter_source_doc_id=chapter_source_doc_id,
                library_chapter_id=library_chapter_id,
            )
            batch_fingerprint = _fingerprint(snapshot_units, chapter_id, chapter_source_doc_id, instructions)
            batch_id = f"recall-batch-{uuid.uuid4().hex[:16]}"
            task_message = message or f"生成章节记忆卡（{len(snapshot_units)} 个知识点）"
            snapshot = {
                "schema_version": 1,
                "request_id": request_id,
                "batch_fingerprint": batch_fingerprint,
                "chapter_id": chapter_id,
                "chapter_title": chapter["title"],
                "chapter_version": chapter["version"],
                "source_doc_id": chapter_source_doc_id,
                "source_doc_ids": sorted({
                    chunk.doc_id for unit in snapshot_units for chunk in unit.chunks
                }),
                "mode": mode,
                "instructions": instructions,
                "library_chapter_id": library_chapter_id,
                "units": [unit.as_dict() for unit in snapshot_units],
            }

            # Insert the batch first so the task's target has a valid FK in
            # deployments that enforce one from tasks to generation batches.
            _insert_available(
                active_conn,
                "recall_generation_batches",
                {
                    "id": batch_id,
                    "chapter_id": chapter_id,
                    "source_doc_id": chapter_source_doc_id,
                    "request_id": request_id,
                    "request_fingerprint": batch_fingerprint,
                    "input_snapshot_json": json.dumps(snapshot, ensure_ascii=False),
                    "status": "pending",
                    "source_doc_ids_json": json.dumps(snapshot["source_doc_ids"], ensure_ascii=False),
                    "candidate_count": 0,
                    "accepted_count": 0,
                    "rejected_count": 0,
                    "metadata_json": json.dumps(snapshot, ensure_ascii=False),
                },
            )
            task_id = TaskWorker.create_task(
                TASK_TYPE,
                target_id=batch_id,
                message=task_message,
                parent_task_id=parent_task_id,
                conn=active_conn,
            )
            row = active_conn.execute(
                "SELECT * FROM recall_generation_batches WHERE id = ?", (batch_id,)
            ).fetchone()
            return cls._batch_response(row, snapshot, task_id=task_id)

        if conn is not None:
            return _create(conn)
        with db_session() as active_conn:
            return _create(active_conn)

    # ------------------------------------------------------------------
    # Snapshot construction/validation
    # ------------------------------------------------------------------

    @classmethod
    def _build_units(
        cls,
        conn: Any,
        *,
        chapter: Mapping[str, Any],
        units: Optional[Sequence[Any]],
        knowledge_point_ids: Optional[Sequence[str]],
        chapter_source_doc_id: Optional[str],
        library_chapter_id: Optional[str] = None,
    ) -> List[SnapshotUnit]:
        if units is None:
            if knowledge_point_ids is None:
                raise RecallGenerationError("必须提供知识点单元")
            if isinstance(knowledge_point_ids, (str, bytes)):
                knowledge_point_ids = [str(knowledge_point_ids)]
            units = [
                {"knowledge_point_id": str(kp_id)} for kp_id in knowledge_point_ids
            ]
        if isinstance(units, Mapping):
            # A mapping keyed by KP id is a convenient internal form.
            units = [dict(value, knowledge_point_id=key) if isinstance(value, Mapping) else value
                     for key, value in units.items()]
        units = list(units)
        if not MIN_UNITS <= len(units) <= MAX_UNITS:
            raise RecallGenerationError(f"知识点单元数量必须在 {MIN_UNITS}—{MAX_UNITS} 之间")

        result: List[SnapshotUnit] = []
        seen: set[str] = set()
        for raw_unit in units:
            unit = _as_dict(raw_unit)
            kp_id = _normalize_id(
                _first(unit, "knowledge_point_id", "knowledgePointId", "kp_id", "id"),
                field="knowledge_point_id",
            )
            if kp_id in seen:
                raise RecallGenerationError(f"知识点单元重复: {kp_id}")
            seen.add(kp_id)
            kp_row = conn.execute(
                "SELECT id, name, chapter, is_deleted, source_reference, source_status "
                "FROM knowledge_points WHERE id = ?",
                (kp_id,),
            ).fetchone()
            if not kp_row:
                raise RecallGenerationError(f"知识点不存在: {kp_id}")
            if kp_row["is_deleted"]:
                raise RecallGenerationError(f"知识点已删除: {kp_id}")
            kp_row = dict(kp_row)

            unit_chapter_id = _first(unit, "chapter_id", "chapterId")
            if unit_chapter_id is not None and str(unit_chapter_id) != str(chapter["id"]):
                raise RecallGenerationError(f"知识点 {kp_id} 不属于所选章节")
            unit_chapter = _first(unit, "chapter", "chapter_title", "chapterTitle")
            if not library_chapter_id:
                cls._validate_chapter_text(kp_id, kp_row["chapter"], chapter["title"], unit_chapter)

            raw_chunks = _first(unit, "chunks", "source_chunks", "input_chunks")
            scope_fingerprint = None
            if library_chapter_id:
                # Scope is always resolved again by the server. Supplying this
                # identifier never grants permission to bypass source validation.
                from app.services.recall_scope import validated_chunks, chapter_scope
                from app.services.recall_cards import normalize_title
                scope_title, _, scope_fingerprint = chapter_scope(conn, chapter_source_doc_id, library_chapter_id)
                if normalize_title(scope_title) != normalize_title(chapter["title"]) or chapter.get("source_doc_id") != chapter_source_doc_id:
                    raise RecallGenerationError("回忆卡目录与教材章节不一致")
                raw_chunks = validated_chunks(conn, kp_row, chapter_source_doc_id, library_chapter_id)
            elif raw_chunks is None:
                raw_chunks = cls._derive_chunks(conn, kp_row, chapter, chapter_source_doc_id)
            if isinstance(raw_chunks, Mapping):
                raw_chunks = list(raw_chunks.values())
            raw_chunks = list(raw_chunks or [])
            if not MIN_CHUNKS_PER_UNIT <= len(raw_chunks) <= MAX_CHUNKS_PER_UNIT:
                raise RecallGenerationError(
                    f"知识点 {kp_id} 的有效切片数量必须在 {MIN_CHUNKS_PER_UNIT}—{MAX_CHUNKS_PER_UNIT} 之间"
                )

            snapshot_chunks: List[SnapshotChunk] = []
            for raw_chunk in raw_chunks:
                snapshot_chunks.append(
                    cls._snapshot_chunk(
                        conn,
                        raw_chunk,
                        chapter=chapter,
                        chapter_source_doc_id=chapter_source_doc_id,
                    )
                )
            total_chars = sum(len(chunk.content) for chunk in snapshot_chunks)
            if total_chars > MAX_UNIT_CHARS:
                raise RecallGenerationError(f"知识点 {kp_id} 的输入正文超过 {MAX_UNIT_CHARS} 字符限制")
            result.append(
                SnapshotUnit(
                    knowledge_point_id=kp_id,
                    knowledge_point_name=_text(kp_row["name"], field="knowledge_point.name", required=True),
                    knowledge_point_chapter=_text(kp_row["chapter"], field="knowledge_point.chapter") or None,
                    chapter_id=str(chapter["id"]),
                    chapter_title=_text(chapter["title"], field="chapter.title", required=True),
                    source_doc_id=chapter_source_doc_id,
                    chunks=tuple(snapshot_chunks),
                    library_chapter_id=library_chapter_id,
                    library_scope_fingerprint=scope_fingerprint,
                )
            )
        return result

    @staticmethod
    def _validate_chapter_text(
        kp_id: str,
        kp_chapter: Any,
        chapter_title: str,
        supplied_chapter: Any,
    ) -> None:
        values = [str(value).strip() for value in (kp_chapter, supplied_chapter) if value]
        canonical_title = _normalize_whitespace(str(chapter_title))
        if supplied_chapter and _normalize_whitespace(str(supplied_chapter)) != canonical_title:
            raise RecallGenerationError(f"知识点 {kp_id} 的章节字段与章节不一致")
        if values:
            # Existing knowledge-point rows often contain a chapter prefix while
            # recall_chapters.title includes the full title.  Containment handles
            # that representation while still rejecting another chapter.
            if not any(
                _normalize_whitespace(value) == canonical_title
                or _normalize_whitespace(value) in canonical_title
                or canonical_title in _normalize_whitespace(value)
                for value in values
            ):
                raise RecallGenerationError(f"知识点 {kp_id} 不属于所选章节")

    @classmethod
    def _derive_chunks(
        cls,
        conn: Any,
        kp_row: Mapping[str, Any],
        chapter: Mapping[str, Any],
        chapter_source_doc_id: Optional[str],
    ) -> List[Dict[str, Any]]:
        """Derive chunks from explicit source references when callers omit units."""
        kp_row = _as_dict(kp_row)
        if kp_row.get("source_status") not in (None, "valid"):
            raise RecallGenerationError("知识点来源已失效，请先核对原文来源后重新生成")

        # Accepted extraction citations store the chunk-content hash, whereas a
        # generation snapshot stores the document-file hash. Validate the former
        # here, then load canonical rows below rather than confusing both hashes.
        refs: List[str] = []
        candidates = conn.execute(
            "SELECT citations_json, source_status FROM extraction_candidates "
            "WHERE target_kp_id = ? AND status IN ('accepted', 'merged') ORDER BY id",
            (kp_row["id"],),
        ).fetchall()
        for candidate in candidates:
            if candidate["source_status"] not in (None, "valid"):
                raise RecallGenerationError("知识点的已审核提取来源已失效，请先核对原文来源")
            try:
                citations = json.loads(candidate["citations_json"])
            except (TypeError, ValueError) as exc:
                raise RecallGenerationError("知识点的已审核引用格式错误，请核对或重新提取来源") from exc
            if not isinstance(citations, list) or not citations:
                raise RecallGenerationError("知识点的已审核提取结果缺少原文引用，请核对或重新提取来源")
            for citation in citations:
                if not isinstance(citation, dict) or not isinstance(citation.get("chunk_id"), str):
                    raise RecallGenerationError("知识点的已审核引用缺少切片编号，请核对或重新提取来源")
                chunk_id = citation["chunk_id"]
                current = conn.execute(
                    "SELECT c.doc_id, c.content, c.parse_error FROM document_chunks c "
                    "JOIN documents d ON d.id = c.doc_id WHERE c.id = ?", (chunk_id,)
                ).fetchone()
                if not current or current["parse_error"] or not str(current["content"] or "").strip():
                    raise RecallGenerationError("知识点引用的原文切片已缺失或不可用，请重新导入并核对来源")
                if citation.get("doc_id") != current["doc_id"]:
                    raise RecallGenerationError("知识点引用的资料归属不一致，请重新核对来源")
                if chapter_source_doc_id and current["doc_id"] != chapter_source_doc_id:
                    raise RecallGenerationError("知识点引用不属于所选章节资料，请选择正确章节或核对来源")
                content = str(current["content"])
                if citation.get("source_hash") != _sha256_text(content):
                    raise RecallGenerationError("知识点引用的原文已变化，请重新核对来源后生成")
                quote = citation.get("quote")
                if not isinstance(quote, str) or not quote.strip() or quote not in content:
                    raise RecallGenerationError("知识点引用摘录与当前原文不一致，请重新核对来源后生成")
                refs.append(chunk_id)

        raw_reference = kp_row.get("source_reference")
        if raw_reference:
            try:
                parsed = json.loads(raw_reference) if isinstance(raw_reference, str) else raw_reference
            except (TypeError, ValueError):
                parsed = None
            refs.extend(cls._find_chunk_ids(parsed))
        refs = list(dict.fromkeys(refs))
        if refs:
            placeholders = ",".join("?" for _ in refs)
            rows = conn.execute(
                f"SELECT c.id, c.doc_id, c.page_number, c.paragraph_ref, c.heading, c.content, "
                f"c.parse_error, d.filename, d.file_hash "
                f"FROM document_chunks c JOIN documents d ON c.doc_id = d.id "
                f"WHERE c.id IN ({placeholders}) ORDER BY c.chunk_index", refs
            ).fetchall()
            if len(rows) != len(refs):
                raise RecallGenerationError("知识点来源引用了不存在的切片，请重新核对来源")
            return [dict(row) for row in rows]

        if raw_reference:
            raise RecallGenerationError("知识点来源仅有展示说明，无法确定原文切片，请核对或重新提取来源")
        if not chapter_source_doc_id:
            raise RecallGenerationError("知识点没有可推导的章节资料来源，请先关联教材并核对来源")
        # Prefer chunks whose heading matches the chapter.  If the importer did
        # not preserve headings, use all valid chunks only when the cardinality is
        # already within the strict per-unit bound.
        rows = conn.execute(
            "SELECT c.id, c.doc_id, c.page_number, c.paragraph_ref, c.heading, c.content, "
            "c.parse_error, d.filename, d.file_hash "
            "FROM document_chunks c JOIN documents d ON c.doc_id = d.id "
            "WHERE c.doc_id = ? AND c.parse_error IS NULL AND TRIM(c.content) <> '' "
            "ORDER BY c.chunk_index",
            (chapter_source_doc_id,),
        ).fetchall()
        title_norm = _normalize_whitespace(str(chapter["title"])).lower()
        headed = [
            dict(row) for row in rows
            if row["heading"] and (
                title_norm in _normalize_whitespace(str(row["heading"])).lower()
                or _normalize_whitespace(str(row["heading"])).lower() in title_norm
            )
        ]
        selected = headed or [dict(row) for row in rows]
        if len(selected) > MAX_CHUNKS_PER_UNIT:
            raise RecallGenerationError(
                "未提供知识点来源切片，无法在严格 10 个切片限制内确定章节归属"
            )
        return selected

    @staticmethod
    def _find_chunk_ids(value: Any) -> List[str]:
        found: List[str] = []
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key in {"chunk_id", "source_chunk_id", "chunkId"} and item:
                    found.append(str(item))
                elif key in {"chunk_ids", "source_chunk_ids", "chunkIds"} and isinstance(item, (list, tuple)):
                    found.extend(str(x) for x in item if x)
                else:
                    found.extend(RecallGenerator._find_chunk_ids(item))
        elif isinstance(value, (list, tuple)):
            for item in value:
                found.extend(RecallGenerator._find_chunk_ids(item))
        return found

    @classmethod
    def _snapshot_chunk(
        cls,
        conn: Any,
        raw_chunk: Any,
        *,
        chapter: Mapping[str, Any],
        chapter_source_doc_id: Optional[str],
    ) -> SnapshotChunk:
        chunk = _as_dict(raw_chunk)
        chunk_id = _normalize_id(_first(chunk, "id", "chunk_id", "chunkId"), field="chunk_id")
        row = conn.execute(
            "SELECT c.id, c.doc_id, c.page_number, c.paragraph_ref, c.heading, c.content, "
            "c.parse_error, d.filename, d.file_hash "
            "FROM document_chunks c JOIN documents d ON c.doc_id = d.id WHERE c.id = ?",
            (chunk_id,),
        ).fetchone()
        if not row:
            raise RecallGenerationError(f"来源切片不存在: {chunk_id}")
        if row["parse_error"] or not row["content"] or not str(row["content"]).strip():
            raise RecallGenerationError(f"来源切片无效: {chunk_id}")
        if chapter_source_doc_id and row["doc_id"] != chapter_source_doc_id:
            raise RecallGenerationError(f"切片 {chunk_id} 不属于章节资料")
        supplied_doc_id = _first(chunk, "doc_id", "source_doc_id", "material_id", "source_material_id")
        if supplied_doc_id is not None and str(supplied_doc_id) != str(row["doc_id"]):
            raise RecallGenerationError(f"切片 {chunk_id} 的资料归属不一致")
        supplied_content = _first(chunk, "content", "text")
        if supplied_content is not None and str(supplied_content) != str(row["content"]):
            # Do not permit callers to smuggle text into the immutable snapshot.
            raise RecallGenerationError(f"切片 {chunk_id} 的正文已变化，请重新读取资料")
        source_hash = str(row["file_hash"] or "").strip()
        supplied_hash = _first(chunk, "source_hash", "document_hash", "hash", "file_hash")
        if supplied_hash is not None and str(supplied_hash) != source_hash:
            raise RecallGenerationError(f"切片 {chunk_id} 的来源哈希不匹配")
        content = str(row["content"])
        return SnapshotChunk(
            id=str(row["id"]),
            doc_id=str(row["doc_id"]),
            doc_name=str(row["filename"]),
            source_hash=source_hash,
            content_hash=_sha256_text(content),
            content=content,
            page_number=row["page_number"],
            paragraph_ref=row["paragraph_ref"],
            heading=row["heading"],
        )

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    @classmethod
    async def execute_generation_task(cls, task_id: str, batch_id: str) -> Dict[str, Any]:
        """Run a persisted batch.  TaskWorker owns terminal task transitions."""
        if TaskWorker.is_cancelled(task_id):
            cls._mark_batch(batch_id, "cancelled", "任务在开始前已取消")
            TaskWorker.finalize_cancellation(task_id)
            return {"status": "cancelled", "batch_id": batch_id}

        with db_session() as conn:
            batch_row = conn.execute(
                "SELECT * FROM recall_generation_batches WHERE id = ?", (batch_id,)
            ).fetchone()
        if not batch_row:
            TaskWorker.mark_task_failed(task_id, "记忆卡生成批次不存在")
            return {"status": "failed", "batch_id": batch_id, "error": "batch_not_found"}
        # New schema stores the snapshot in input_snapshot_json and older
        # additive shapes use metadata_json.  Prefer the explicit snapshot.
        metadata = cls._decode_json(_row_value(batch_row, "metadata_json"), {})
        input_snapshot = cls._decode_json(_row_value(batch_row, "input_snapshot_json"), {})
        if not isinstance(input_snapshot, Mapping) or not input_snapshot.get("units"):
            input_snapshot = metadata
        if not isinstance(input_snapshot, Mapping):
            input_snapshot = {}
        metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
        metadata.update({key: value for key, value in input_snapshot.items() if key not in metadata or not metadata.get(key)})
        try:
            units = cls._load_snapshot_units(metadata)
            request_id = _normalize_id(
                metadata.get("request_id") or _row_value(batch_row, "request_id"),
                field="request_id",
            )
            fingerprint = _normalize_id(
                metadata.get("batch_fingerprint")
                or metadata.get("request_fingerprint")
                or _row_value(batch_row, "request_fingerprint"),
                field="batch_fingerprint",
            )
            mode = _normalise_mode(metadata.get("mode"))
            if mode == "formal" and _demo_enabled():
                raise RecallGenerationError("正式模式禁止使用 DEMO_MODE 离线生成")
        except Exception as exc:
            cls._mark_batch(batch_id, "failed", str(exc))
            TaskWorker.mark_task_failed(task_id, str(exc))
            return {"status": "failed", "batch_id": batch_id, "error": str(exc)}

        # An interrupted process may have no in-memory task detail.  Rehydrate the
        # important progress fields before processing units.
        TaskWorker.update_progress_detail(
            task_id,
            stage="generate_recall_cards",
            phase="working",
            unit="knowledge_point",
            total=len(units),
            succeeded=0,
            failed=0,
            skipped=0,
            resultCount=0,
            currentAction="准备生成记忆卡...",
            force=True,
        )
        with db_session() as conn:
            conn.execute(
                "UPDATE recall_generation_batches SET status = 'processing', updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND status IN ('pending', 'processing', 'partial')",
                (batch_id,),
            )

        checkpoints = cls._get_checkpoints(batch_id)
        succeeded = 0
        failed = 0
        skipped = 0
        card_count = 0
        errors: List[str] = []

        for index, unit in enumerate(units, start=1):
            unit_id = f"kp-{unit.knowledge_point_id}"
            scope = unit.knowledge_point_name
            if TaskWorker.is_cancelled(task_id):
                await cls._cancel_batch(
                    task_id,
                    batch_id,
                    unit=unit,
                    unit_index=index,
                    request_id=request_id,
                    fingerprint=fingerprint,
                )
                return {"status": "cancelled", "batch_id": batch_id, "card_count": card_count}

            # Completed checkpoints are reusable only for the exact immutable
            # fingerprint.  Unit key is intentionally the knowledge_point_id.
            cp = checkpoints.get(unit.knowledge_point_id)
            if cp and cls._checkpoint_matches(cp, unit, fingerprint):
                skipped += 1
                succeeded += 1
                card_count += int(cp.get("result_count") or 0)
                TaskWorker.upsert_exec_unit(
                    task_id,
                    unit_id,
                    kind="knowledge_point",
                    index=index,
                    scope=scope,
                    state="skipped",
                    action="已从检查点恢复",
                    force=True,
                )
                TaskWorker.finish_exec_unit(task_id, unit_id, state="skipped", force=True)
                TaskWorker.log_event(
                    task_id,
                    "checkpoint_restored",
                    f"知识点 {scope} 命中历史检查点，跳过重复生成",
                    stage="generate_recall_cards",
                    unit_index=index,
                )
                TaskWorker.update_progress_detail(
                    task_id,
                    succeeded=succeeded,
                    skipped=skipped,
                    resultCount=card_count,
                    currentAction=f"已恢复 {scope}",
                    force=True,
                )
                continue

            TaskWorker.upsert_exec_unit(
                task_id,
                unit_id,
                kind="knowledge_point",
                index=index,
                scope=scope,
                service="llm",
                state="waiting_slot",
                action="等待模型调用名额",
                force=True,
            )
            TaskWorker.update_progress_detail(
                task_id,
                phase="waiting_provider",
                currentAction=f"正在生成 {scope} 的记忆卡...",
                force=True,
            )
            on_event = TaskWorker.make_unit_event_handler(
                task_id,
                unit_id,
                label=scope,
                stage="generate_recall_cards",
                unit_index=index,
                service="llm",
            )
            try:
                cls._assert_snapshot_current(unit)
                user_prompt = cls._build_user_prompt(
                    unit,
                    request_id=request_id,
                    fingerprint=fingerprint,
                    mode=mode,
                    instructions=metadata.get("instructions", ""),
                )
                response = await LLMClient.chat_json(
                    RECALL_CARD_GENERATION_SYSTEM_PROMPT,
                    user_prompt,
                    on_event=on_event,
                )
                if TaskWorker.is_cancelled(task_id):
                    await cls._cancel_batch(
                        task_id,
                        batch_id,
                        unit=unit,
                        unit_index=index,
                        request_id=request_id,
                        fingerprint=fingerprint,
                    )
                    return {"status": "cancelled", "batch_id": batch_id, "card_count": card_count}
                TaskWorker.upsert_exec_unit(
                    task_id,
                    unit_id,
                    state="saving",
                    action="正在严格校验并保存候选卡...",
                    force=True,
                )
                cards, insufficient_reason = cls.validate_model_output(response, unit)
                cls._assert_snapshot_current(unit)
                saved = cls._persist_unit(
                    task_id=task_id,
                    batch_id=batch_id,
                    unit=unit,
                    cards=cards,
                    insufficient_reason=insufficient_reason,
                    request_id=request_id,
                    fingerprint=fingerprint,
                )
                card_count += saved
                succeeded += 1
                TaskWorker.finish_exec_unit(task_id, unit_id, state="succeeded", force=True)
                TaskWorker.update_progress_detail(
                    task_id,
                    succeeded=succeeded,
                    failed=failed,
                    skipped=skipped,
                    resultCount=card_count,
                    currentAction=f"{scope} 生成完成（{saved} 张候选卡）",
                    force=True,
                )
            except asyncio.CancelledError:
                await cls._cancel_batch(
                    task_id,
                    batch_id,
                    unit=unit,
                    unit_index=index,
                    request_id=request_id,
                    fingerprint=fingerprint,
                )
                raise
            except SourceChangedError as exc:
                failed += 1
                errors.append(f"{scope}: 来源已变化")
                cls._save_unit_checkpoint(
                    batch_id,
                    unit,
                    status="source_changed",
                    result_count=0,
                    error_msg=str(exc),
                    request_id=request_id,
                    fingerprint=fingerprint,
                )
                TaskWorker.finish_exec_unit(task_id, unit_id, state="failed", error=str(exc), force=True)
                TaskWorker.update_progress_detail(
                    task_id,
                    succeeded=succeeded,
                    failed=failed,
                    skipped=skipped,
                    currentAction=f"{scope} 来源已变化，未保存候选卡",
                    force=True,
                )
            except Exception as exc:
                failed += 1
                safe_error = TaskWorker.sanitize_error(exc) or type(exc).__name__
                errors.append(f"{scope}: {safe_error}")
                cls._save_unit_checkpoint(
                    batch_id,
                    unit,
                    status="failed",
                    result_count=0,
                    error_msg=safe_error,
                    request_id=request_id,
                    fingerprint=fingerprint,
                )
                TaskWorker.finish_exec_unit(task_id, unit_id, state="failed", error=safe_error, force=True)
                TaskWorker.update_progress_detail(
                    task_id,
                    succeeded=succeeded,
                    failed=failed,
                    skipped=skipped,
                    currentAction=f"{scope} 生成失败",
                    force=True,
                )
                logger.warning("Recall card unit failed (%s): %s", unit.knowledge_point_id, safe_error)

        if TaskWorker.is_cancelled(task_id):
            cls._mark_batch(batch_id, "cancelled", "任务已取消")
            TaskWorker.finalize_cancellation(task_id)
            return {"status": "cancelled", "batch_id": batch_id, "card_count": card_count}

        if failed and succeeded:
            status = "partial"
            cls._mark_batch(batch_id, status, "；".join(errors))
            with db_session() as conn:
                conn.execute(
                    "UPDATE tasks SET batch_errors_json = ? WHERE id = ?",
                    (json.dumps(errors, ensure_ascii=False), task_id),
                )
            TaskWorker.mark_task_partial(task_id, f"记忆卡生成部分完成（成功 {succeeded}/{len(units)} 个知识点）")
        elif failed:
            status = "failed"
            cls._mark_batch(batch_id, status, "；".join(errors))
            with db_session() as conn:
                conn.execute(
                    "UPDATE tasks SET batch_errors_json = ? WHERE id = ?",
                    (json.dumps(errors, ensure_ascii=False), task_id),
                )
            TaskWorker.mark_task_failed(task_id, "记忆卡生成全部失败；" + "；".join(errors))
        else:
            status = "completed"
            cls._mark_batch(batch_id, status, None)
            TaskWorker.mark_task_completed(task_id, f"记忆卡候选生成完成，共 {card_count} 张，待人工审核")
        return {
            "status": status,
            "batch_id": batch_id,
            "succeeded_units": succeeded,
            "failed_units": failed,
            "skipped_units": skipped,
            "card_count": card_count,
            "errors": errors,
        }

    # Compatibility names used by service/worker implementations.
    execute = execute_generation_task
    execute_task = execute_generation_task
    generate = execute_generation_task
    run = execute_generation_task

    @classmethod
    def _build_user_prompt(
        cls,
        unit: SnapshotUnit,
        *,
        request_id: str,
        fingerprint: str,
        mode: str,
        instructions: str = "",
    ) -> str:
        payload = {
            "request_id": request_id,
            "batch_fingerprint": fingerprint,
            "mode": mode,
            "instructions": instructions,
            "knowledge_point_id": unit.knowledge_point_id,
            "knowledge_point_name": unit.knowledge_point_name,
            "chapter_id": unit.chapter_id,
            "chapter_title": unit.chapter_title,
            "source_doc_id": unit.source_doc_id,
            "source_chunks": [chunk.as_dict() for chunk in unit.chunks],
        }
        return (
            "以下是不可变的单知识点输入快照。只能依据 source_chunks 生成；"
            "严禁使用外部知识、演示文本或虚构引用。\n"
            "在原文证据范围内遵循 instructions 中的生成要求；"
            "不得因额外要求放宽引用与审核规则。\n"
            "模型输入 JSON：\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
        )

    @classmethod
    def validate_model_output(
        cls,
        response: Any,
        unit: SnapshotUnit | Mapping[str, Any],
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Validate one unit atomically; invalid cards never reach persistence."""
        if not isinstance(unit, SnapshotUnit):
            unit = cls._unit_from_snapshot(_as_dict(unit))
        data = _as_dict(response) if isinstance(response, Mapping) else None
        if data is None:
            raise RecallGenerationError("模型返回必须是 JSON 对象")

        # A batch-shaped mock/provider response is accepted, but only the matching
        # unit is considered.  Unknown/mismatched IDs are rejected, not remapped.
        if isinstance(data.get("units"), list):
            candidates = [item for item in data["units"] if isinstance(item, Mapping)]
            matching = [
                dict(item) for item in candidates
                if str(_first(item, "knowledge_point_id", "knowledgePointId", "kp_id", default=""))
                == unit.knowledge_point_id
            ]
            if not matching:
                raise RecallGenerationError("模型返回缺少当前知识点单元")
            if len(matching) != 1:
                raise RecallGenerationError("模型重复返回当前知识点单元")
            data = matching[0]

        raw_cards = _first(data, "cards", "recall_cards", "candidates", "items")
        if raw_cards is None and any(key in data for key in ("question", "front", "front_content")):
            raw_cards = [data]
        if raw_cards is None:
            raw_cards = []
        if not isinstance(raw_cards, list):
            raise RecallGenerationError("模型返回 cards 必须是数组")
        if len(raw_cards) > MAX_CARDS_PER_UNIT:
            raise RecallGenerationError(f"单知识点卡片数量不得超过 {MAX_CARDS_PER_UNIT} 张")

        reason_raw = _first(data, "insufficient_reason", "insufficientReason", "reason")
        reason = None if reason_raw in (None, "") else _text(
            reason_raw,
            field="insufficient_reason",
            required=True,
            max_chars=MAX_INSUFFICIENT_REASON_CHARS,
        )
        if raw_cards and reason:
            raise RecallGenerationError("有卡片时不得填写 insufficient_reason")
        if not raw_cards and not reason:
            raise RecallGenerationError("无卡片时必须说明 insufficient_reason")

        chunks = {chunk.id: chunk for chunk in unit.chunks}
        validated: List[Dict[str, Any]] = []
        for card_index, raw_card in enumerate(raw_cards, start=1):
            if not isinstance(raw_card, Mapping):
                raise RecallGenerationError(f"第 {card_index} 张卡片不是对象")
            card = dict(raw_card)
            title = _text(
                _first(card, "title", "name", default=unit.knowledge_point_name),
                field=f"第 {card_index} 张卡片 title",
                required=True,
                max_chars=MAX_CARD_TITLE_CHARS,
            )
            front = _text(
                _first(card, "front_content", "front", "question", "prompt", "stem"),
                field=f"第 {card_index} 张卡片 front",
                required=True,
                max_chars=MAX_CARD_FRONT_CHARS,
            )
            back = _text(
                _first(card, "back_content", "back", "answer", "response", "explanation"),
                field=f"第 {card_index} 张卡片 back",
                required=True,
                max_chars=MAX_CARD_BACK_CHARS,
            )
            card_type = _text(
                _first(card, "card_type", "type", default="basic"),
                field=f"第 {card_index} 张卡片 card_type",
                required=True,
                max_chars=MAX_CARD_TYPE_CHARS,
            )
            raw_citations = _first(card, "citations", "source_citations", "sources")
            if not isinstance(raw_citations, list) or not (1 <= len(raw_citations) <= MAX_CITATIONS_PER_CARD):
                raise RecallGenerationError(
                    f"第 {card_index} 张卡片必须有 1—{MAX_CITATIONS_PER_CARD} 条引用"
                )
            citations: List[Dict[str, Any]] = []
            seen_citations: set[Tuple[str, str]] = set()
            for citation_index, raw_citation in enumerate(raw_citations, start=1):
                if not isinstance(raw_citation, Mapping):
                    raise RecallGenerationError(f"第 {card_index} 张卡片第 {citation_index} 条引用无效")
                citation = dict(raw_citation)
                chunk_id = _text(
                    _first(citation, "chunk_id", "chunkId", "source_chunk_id"),
                    field="citation.chunk_id",
                    required=True,
                    max_chars=300,
                )
                if chunk_id not in chunks:
                    raise RecallGenerationError(f"引用了不属于当前快照的切片: {chunk_id}")
                quote = _text(
                    _first(citation, "quote", "snippet", "excerpt"),
                    field="citation.quote",
                    required=True,
                    max_chars=MAX_QUOTE_CHARS,
                )
                if not _quote_matches(quote, chunks[chunk_id].content):
                    raise RecallGenerationError(f"引用原文不匹配切片: {chunk_id}")
                emitted_hash = _text(
                    _first(citation, "source_hash", "document_hash", "hash", "file_hash"),
                    field="citation.source_hash",
                    required=True,
                    max_chars=300,
                )
                if emitted_hash not in {
                    chunks[chunk_id].source_hash,
                    chunks[chunk_id].content_hash,
                }:
                    raise RecallGenerationError(f"引用来源哈希不匹配切片: {chunk_id}")
                key = (chunk_id, quote)
                if key in seen_citations:
                    raise RecallGenerationError("同一张卡片存在重复引用")
                seen_citations.add(key)
                citations.append(
                    {
                        "chunk_id": chunk_id,
                        "doc_id": chunks[chunk_id].doc_id,
                        "doc_name": chunks[chunk_id].doc_name,
                        "source_hash": emitted_hash,
                        "content_hash": chunks[chunk_id].content_hash,
                        "page_number": chunks[chunk_id].page_number,
                        "paragraph_ref": chunks[chunk_id].paragraph_ref,
                        "quote": quote,
                    }
                )
            validated.append(
                {
                    "title": title,
                    "card_type": card_type,
                    "front_content": front,
                    "back_content": back,
                    # Keep aliases in content_json for clients using either name.
                    "question": front,
                    "answer": back,
                    "citations": citations,
                }
            )
        return validated, reason

    # ------------------------------------------------------------------
    # Persistence/checkpoints/source change
    # ------------------------------------------------------------------

    @classmethod
    def _persist_unit(
        cls,
        *,
        task_id: str,
        batch_id: str,
        unit: SnapshotUnit,
        cards: Sequence[Mapping[str, Any]],
        insufficient_reason: Optional[str],
        request_id: str,
        fingerprint: str,
    ) -> int:
        """Persist a fully validated unit and its checkpoint in one transaction."""
        with db_session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # The task status check is made in the same transaction as inserts;
            # cancellation therefore cannot race a successful card write.
            task_row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task_row and task_row["status"] in ("cancel_requested", "cancelled"):
                raise asyncio.CancelledError("任务已请求取消")
            cls._assert_snapshot_current(unit, conn=conn)
            card_ids: List[str] = []
            for position, card in enumerate(cards):
                card_id = f"recall-card-{uuid.uuid4().hex[:16]}"
                revision_id = f"recall-revision-{uuid.uuid4().hex[:16]}"
                metadata = {
                    "request_id": request_id,
                    "batch_fingerprint": fingerprint,
                    "knowledge_point_id": unit.knowledge_point_id,
                    "chapter_id": unit.chapter_id,
                    "candidate_status": "pending",
                    "insufficient_reason": insufficient_reason,
                    "source_snapshot": {
                        "source_doc_id": unit.source_doc_id,
                        "source_hashes": unit.source_hashes,
                        "chunk_ids": [chunk.id for chunk in unit.chunks],
                    },
                }
                content = dict(card)
                citations = list(content.get("citations") or [])
                _insert_available(
                    conn,
                    "recall_cards",
                    {
                        "id": card_id,
                        "chapter_id": unit.chapter_id,
                        "source_doc_id": unit.source_doc_id,
                        "generation_batch_id": batch_id,
                        "title": content["title"],
                        "prompt": content["front_content"],
                        "answer": content["back_content"],
                        "card_type": content["card_type"],
                        "knowledge_point_id": unit.knowledge_point_id,
                        "origin": "generated",
                        "status": "draft",
                        "publication_status": "draft",
                        "is_confirmed": 0,
                        "is_ai_generated": 1,
                        "candidate": 1,
                        "sort_order": position,
                        "version": 1,
                        "metadata_json": json.dumps(metadata, ensure_ascii=False),
                    },
                )
                content_json = json.dumps(
                    {
                        **content,
                        "knowledge_point_id": unit.knowledge_point_id,
                        "request_id": request_id,
                        "batch_fingerprint": fingerprint,
                        "candidate_status": "pending",
                    },
                    ensure_ascii=False,
                )
                _insert_available(
                    conn,
                    "recall_card_revisions",
                    {
                        "id": revision_id,
                        "card_id": card_id,
                        "revision_number": 1,
                        "status": "draft",
                        "content_json": content_json,
                        "concise_content": content["front_content"],
                        "detailed_content": content["back_content"],
                        "front_content": content["front_content"],
                        "back_content": content["back_content"],
                        "prompt": content["front_content"],
                        "hint": None,
                        "source_excerpt": None,
                        "tags_json": "[]",
                        "answer": content["answer"],
                        "question": content["question"],
                        "answer_structure": None,
                        "confusing_points_and_cases": None,
                        "source_citations_json": json.dumps(citations, ensure_ascii=False),
                        "created_by": "llm",
                        "review_status": "draft",
                        "review_note": None,
                        "revision_no": 1,
                    },
                )
                card_columns = _table_columns(conn, "recall_cards")
                if "current_revision_id" in card_columns:
                    conn.execute(
                        "UPDATE recall_cards SET current_revision_id = ? WHERE id = ?",
                        (revision_id, card_id),
                    )
                elif "current_revision_no" in card_columns:
                    conn.execute(
                        "UPDATE recall_cards SET current_revision_no = 1 WHERE id = ?",
                        (card_id,),
                    )
                card_ids.append(card_id)
            checkpoint_data = {
                "request_id": request_id,
                "batch_fingerprint": fingerprint,
                "knowledge_point_id": unit.knowledge_point_id,
                "source_hashes": unit.source_hashes,
                "chunk_ids": [chunk.id for chunk in unit.chunks],
                "card_ids": card_ids,
                "insufficient_reason": insufficient_reason,
            }
            TaskWorker.save_checkpoint(
                TASK_TYPE,
                batch_id,
                unit.knowledge_point_id,
                0,
                "completed",
                result_count=len(card_ids),
                data=checkpoint_data,
                model_version=getattr(settings, "LLM_MODEL", None),
                conn=conn,
            )
            conn.execute(
                "UPDATE recall_generation_batches SET candidate_count = candidate_count + ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (len(card_ids), batch_id),
            )
            return len(card_ids)

    @classmethod
    def _save_unit_checkpoint(
        cls,
        batch_id: str,
        unit: SnapshotUnit,
        *,
        status: str,
        result_count: int,
        error_msg: Optional[str],
        request_id: str,
        fingerprint: str,
    ) -> None:
        try:
            TaskWorker.save_checkpoint(
                TASK_TYPE,
                batch_id,
                unit.knowledge_point_id,
                0,
                status,
                result_count=result_count,
                data={
                    "request_id": request_id,
                    "batch_fingerprint": fingerprint,
                    "knowledge_point_id": unit.knowledge_point_id,
                    "source_hashes": unit.source_hashes,
                    "chunk_ids": [chunk.id for chunk in unit.chunks],
                },
                error_msg=error_msg,
                model_version=getattr(settings, "LLM_MODEL", None),
            )
        except Exception as exc:  # checkpoint failure must not erase task status
            logger.warning("Failed to save recall checkpoint: %s", exc)

    @classmethod
    async def _cancel_batch(
        cls,
        task_id: str,
        batch_id: str,
        *,
        unit: SnapshotUnit,
        unit_index: int,
        request_id: str,
        fingerprint: str,
    ) -> None:
        cls._save_unit_checkpoint(
            batch_id,
            unit,
            status="cancelled",
            result_count=0,
            error_msg="任务已取消",
            request_id=request_id,
            fingerprint=fingerprint,
        )
        cls._mark_batch(batch_id, "cancelled", "任务已取消")
        TaskWorker.log_event(
            task_id,
            "cancelled",
            f"在知识点 {unit.knowledge_point_name} 处取消，未写入当前单元",
            stage="generate_recall_cards",
            unit_index=unit_index,
        )
        TaskWorker.finalize_cancellation(task_id)

    @classmethod
    def _assert_snapshot_current(cls, unit: SnapshotUnit, conn: Optional[Any] = None) -> None:
        def _check(active_conn: Any) -> None:
            if unit.library_chapter_id:
                from app.services.recall_scope import chapter_scope, validated_chunks
                from app.services.recall_cards import normalize_title
                try:
                    directory = active_conn.execute(
                        "SELECT source_doc_id,status,title FROM recall_chapters WHERE id=?", (unit.chapter_id,)
                    ).fetchone()
                    if not directory:
                        raise RecallGenerationError("目标回忆卡目录已删除")
                    if directory["source_doc_id"] != unit.source_doc_id:
                        raise RecallGenerationError("目标回忆卡目录的教材归属已变化")
                    if directory["status"] not in (None, "active"):
                        raise RecallGenerationError("目标回忆卡目录已归档，已停止写入")
                    if normalize_title(directory["title"]) != normalize_title(unit.chapter_title):
                        raise RecallGenerationError("目标回忆卡目录名称已变化")
                    title, _, scope_fp = chapter_scope(active_conn, unit.source_doc_id, unit.library_chapter_id)
                    kp = active_conn.execute("SELECT * FROM knowledge_points WHERE id=? AND is_deleted=0", (unit.knowledge_point_id,)).fetchone()
                    if not kp:
                        raise RecallGenerationError("知识点已删除")
                    current_sources = validated_chunks(active_conn, dict(kp), unit.source_doc_id, unit.library_chapter_id)
                    if normalize_title(title) != normalize_title(unit.chapter_title) or scope_fp != unit.library_scope_fingerprint or {c["id"] for c in current_sources} != {c.id for c in unit.chunks}:
                        raise RecallGenerationError("教材章节归属或知识点来源已变化")
                except (RecallGenerationError, HTTPException) as exc:
                    raise SourceChangedError(str(getattr(exc, "detail", str(exc)))) from exc
            placeholders = ",".join("?" for _ in unit.chunks)
            rows = active_conn.execute(
                f"SELECT c.id, c.doc_id, c.content, c.parse_error, d.file_hash "
                f"FROM document_chunks c JOIN documents d ON c.doc_id = d.id "
                f"WHERE c.id IN ({placeholders})",
                [chunk.id for chunk in unit.chunks],
            ).fetchall()
            current = {str(row["id"]): row for row in rows}
            for snapshot_chunk in unit.chunks:
                row = current.get(snapshot_chunk.id)
                if not row or row["parse_error"] or row["doc_id"] != snapshot_chunk.doc_id:
                    raise SourceChangedError(f"来源切片已删除或失效: {snapshot_chunk.id}")
                if str(row["file_hash"] or "") != snapshot_chunk.source_hash:
                    raise SourceChangedError(f"来源资料哈希已变化: {snapshot_chunk.id}")
                if str(row["content"]) != snapshot_chunk.content:
                    raise SourceChangedError(f"来源切片正文已变化: {snapshot_chunk.id}")

        if conn is not None:
            _check(conn)
        else:
            with db_session() as active_conn:
                _check(active_conn)

    @classmethod
    def _get_checkpoints(cls, batch_id: str) -> Dict[str, Dict[str, Any]]:
        with db_session() as conn:
            rows = conn.execute(
                "SELECT unit_key, unit_index, status, result_count, data_json, error_msg, model_version "
                "FROM task_checkpoints WHERE task_type = ? AND target_id = ? AND status = 'completed'",
                (TASK_TYPE, batch_id),
            ).fetchall()
        result: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            item["data"] = cls._decode_json(item.get("data_json"), {})
            result[str(item["unit_key"])] = item
        return result

    @staticmethod
    def _checkpoint_matches(cp: Mapping[str, Any], unit: SnapshotUnit, fingerprint: str) -> bool:
        data = cp.get("data") if isinstance(cp.get("data"), Mapping) else None
        if data is None:
            raw = cp.get("data_json")
            data = RecallGenerator._decode_json(raw, {})
        return (
            data.get("batch_fingerprint") == fingerprint
            and data.get("knowledge_point_id") == unit.knowledge_point_id
            and data.get("source_hashes") == unit.source_hashes
        )

    @classmethod
    def _mark_batch(cls, batch_id: str, status: str, error: Optional[str]) -> None:
        with db_session() as conn:
            conn.execute(
                "UPDATE recall_generation_batches SET status = ?, error_message = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, error, batch_id),
            )

    @staticmethod
    def _decode_json(raw: Any, default: Any) -> Any:
        if not raw:
            return default
        try:
            value = json.loads(raw) if isinstance(raw, str) else raw
            return value if value is not None else default
        except Exception:
            return default

    @classmethod
    def _load_snapshot_units(cls, metadata: Mapping[str, Any]) -> List[SnapshotUnit]:
        raw_units = metadata.get("units")
        if not isinstance(raw_units, list) or not MIN_UNITS <= len(raw_units) <= MAX_UNITS:
            raise RecallGenerationError("批次输入快照缺失或单元数量越界")
        units = [cls._unit_from_snapshot(_as_dict(raw)) for raw in raw_units]
        if len({unit.knowledge_point_id for unit in units}) != len(units):
            raise RecallGenerationError("批次输入快照存在重复知识点")
        return units

    @staticmethod
    def _unit_from_snapshot(raw: Mapping[str, Any]) -> SnapshotUnit:
        chunks: List[SnapshotChunk] = []
        for raw_chunk in raw.get("chunks") or raw.get("source_chunks") or []:
            chunk = _as_dict(raw_chunk)
            # Source text is hashed and compared verbatim. Generic text-field
            # trimming changes valid OCR/imported whitespace and invalidates our
            # own persisted snapshot before the worker can call the model.
            content = chunk.get("content")
            if not isinstance(content, str) or not content.strip():
                raise RecallGenerationError("snapshot.chunk.content必须是非空正文字符串")
            chunk_id = _normalize_id(_first(chunk, "id", "chunk_id"), field="snapshot.chunk.id")
            source_hash = _text(
                _first(chunk, "source_hash", "document_hash", "file_hash"),
                field="snapshot.chunk.source_hash",
                required=True,
            )
            content_hash = _text(
                _first(chunk, "content_hash", default=_sha256_text(content)),
                field="snapshot.chunk.content_hash",
                required=True,
            )
            if content_hash != _sha256_text(content):
                raise RecallGenerationError(f"输入快照切片哈希不匹配: {chunk_id}")
            chunks.append(
                SnapshotChunk(
                    id=chunk_id,
                    doc_id=_normalize_id(_first(chunk, "doc_id", "source_doc_id"), field="snapshot.chunk.doc_id"),
                    doc_name=_text(_first(chunk, "doc_name", "filename", default=""), field="snapshot.chunk.doc_name"),
                    source_hash=source_hash,
                    content_hash=content_hash,
                    content=content,
                    page_number=chunk.get("page_number"),
                    paragraph_ref=chunk.get("paragraph_ref"),
                    heading=chunk.get("heading"),
                )
            )
        if not MIN_CHUNKS_PER_UNIT <= len(chunks) <= MAX_CHUNKS_PER_UNIT:
            raise RecallGenerationError("输入快照切片数量越界")
        if sum(len(chunk.content) for chunk in chunks) > MAX_UNIT_CHARS:
            raise RecallGenerationError("输入快照正文长度越界")
        return SnapshotUnit(
            knowledge_point_id=_normalize_id(
                _first(raw, "knowledge_point_id", "knowledgePointId"), field="snapshot.knowledge_point_id"
            ),
            knowledge_point_name=_text(raw.get("knowledge_point_name"), field="snapshot.knowledge_point_name", required=True),
            knowledge_point_chapter=_text(raw.get("knowledge_point_chapter"), field="snapshot.knowledge_point_chapter") or None,
            chapter_id=_normalize_id(_first(raw, "chapter_id", "chapterId"), field="snapshot.chapter_id"),
            chapter_title=_text(raw.get("chapter_title"), field="snapshot.chapter_title", required=True),
            source_doc_id=(str(raw["source_doc_id"]) if raw.get("source_doc_id") else None),
            chunks=tuple(chunks),
            library_chapter_id=raw.get("library_chapter_id"),
            library_scope_fingerprint=raw.get("library_scope_fingerprint"),
        )

    @classmethod
    def _batch_response(
        cls,
        row: Any,
        metadata: Mapping[str, Any],
        *,
        task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        result = {
            "id": _row_value(row, "id"),
            "batch_id": _row_value(row, "id"),
            "task_id": task_id,
            "chapter_id": _row_value(row, "chapter_id"),
            "source_doc_id": _row_value(row, "source_doc_id"),
            "status": _row_value(row, "status"),
            "request_id": metadata.get("request_id"),
            "batch_fingerprint": metadata.get("batch_fingerprint") or metadata.get("request_fingerprint"),
            "fingerprint": metadata.get("batch_fingerprint") or metadata.get("request_fingerprint"),
            "knowledge_point_ids": [
                unit.get("knowledge_point_id")
                for unit in (metadata.get("units") or [])
                if isinstance(unit, Mapping)
            ],
            "fingerprint": metadata.get("batch_fingerprint") or metadata.get("request_fingerprint"),
            "candidate_count": _row_value(row, "candidate_count", 0) or 0,
            "accepted_count": _row_value(row, "accepted_count", 0) or 0,
            "rejected_count": _row_value(row, "rejected_count", 0) or 0,
            "error_message": _row_value(row, "error_message"),
            "error_msg": _row_value(row, "error_message"),
            "metadata": dict(metadata),
            "created_at": str(_row_value(row, "created_at", "")),
            "updated_at": str(_row_value(row, "updated_at", "")),
        }
        return result


# Function aliases are useful to code that does not need the class namespace.
def create_generation_batch(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return RecallGenerator.create_generation_batch(*args, **kwargs)


async def execute_generation_task(task_id: str, batch_id: str) -> Dict[str, Any]:
    return await RecallGenerator.execute_generation_task(task_id, batch_id)


__all__ = [
    "RecallGenerationError",
    "SourceChangedError",
    "RecallGenerator",
    "SnapshotChunk",
    "SnapshotUnit",
    "create_generation_batch",
    "execute_generation_task",
    "MIN_UNITS",
    "MAX_UNITS",
    "MIN_CHUNKS_PER_UNIT",
    "MAX_CHUNKS_PER_UNIT",
    "MAX_UNIT_CHARS",
    "MAX_CARDS_PER_UNIT",
    "TASK_TYPE",
]
