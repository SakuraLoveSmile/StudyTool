import hashlib
import json
import uuid
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from app.config import settings
from app.database import db_session
from app.models.schemas import (
    DocumentResponse, DocumentChunkResponse, ChunkUpdateRequest,
    TaskResponse, TaskClearRequest, TaskClearResponse,
    TaskRestoreRequest, TaskRestoreResponse, TaskHistoryResponse,
    TaskProgressDetail, TimingCategory, TaskTimingBreakdown,
    TaskEventItem, TaskEventsResponse
)
from app.services.task_worker import TaskWorker

router = APIRouter(prefix="/api/materials", tags=["materials"])


# ---------------------------------------------------------------------------
# Phase F learning/recall lifecycle compatibility
# ---------------------------------------------------------------------------
#
# The learning tables are additive and are owned by another part of the
# application.  Older databases (and a few installations created while the
# learning feature was being developed) may not have all of the tables or
# columns below.  Keep the lifecycle bridge deliberately defensive: inspect
# sqlite metadata first, use only static identifiers, and never let an optional
# learning-table update make a legacy material operation fail.

_RECALL_TABLES = (
    # Legacy source tables are included because the trigger bridge observes
    # worker-owned document/chunk mutations as well as optional learning rows.
    "documents",
    "document_chunks",
    "document_chunks_fts",
    "recall_chapters",
    "recall_generation_batches",
    "recall_cards",
    "recall_card_revisions",
    "learning_progress",
    "learning_day_units",
    "learning_attempts",
    "learning_events",
    "extraction_batches",
    "extraction_candidates",
)


def _learning_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Return whether an optional learning table is present."""
    if table not in _RECALL_TABLES:
        return False
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
            (table,),
        ).fetchone() is not None
    except Exception:
        return False


def _learning_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _learning_table_exists(conn, table):
        return set()
    try:
        return {
            row[1]
            for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        }
    except Exception:
        return set()


def _ensure_learning_column(
    conn: sqlite3.Connection, table: str, column: str, definition: str
) -> bool:
    """Add a small additive compatibility column when the table exists.

    This is intentionally limited to static table/column names in this module;
    it is not a general SQL identifier escape hatch.  The column is useful on
    schema-v6 databases where revisions did not yet expose ``source_status``.
    """
    columns = _learning_columns(conn, table)
    if not columns or column in columns:
        return column in columns
    try:
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}')
        return True
    except Exception:
        # A concurrent initializer may have added it, or a read-only/legacy
        # table may reject ALTER TABLE.  The caller still has status-based
        # fallbacks, so optional compatibility must remain best effort.
        return column in _learning_columns(conn, table)


def _ensure_learning_lifecycle_columns(conn: sqlite3.Connection) -> None:
    """Ensure small additive lifecycle columns exist where possible.

    The canonical schema may add these fields in a later migration.  Keeping
    this bridge additive makes Phase F safe against both the v6 schema and
    early preview schemas without changing any legacy content tables.
    """
    for table in (
        "recall_chapters",
        "recall_generation_batches",
        "recall_cards",
        "recall_card_revisions",
        "extraction_batches",
        "extraction_candidates",
    ):
        if _learning_table_exists(conn, table):
            _ensure_learning_column(conn, table, "source_status", "TEXT DEFAULT 'valid'")
    if _learning_table_exists(conn, "recall_cards"):
        _ensure_learning_column(conn, "recall_cards", "is_paused", "INTEGER NOT NULL DEFAULT 0")
        _ensure_learning_column(conn, "recall_cards", "publication_status", "TEXT DEFAULT 'draft'")


def _ensure_learning_lifecycle_triggers(conn: sqlite3.Connection) -> None:
    """Install narrow SQLite triggers for worker reparses/stale chunk removal.

    ``task_worker.py`` is intentionally owned elsewhere and cannot call the
    lifecycle bridge directly.  These triggers therefore cover the two worker
    mutations which matter here: changing a chunk's parsed text/error and
    deleting a stale chunk.  They are only created when every referenced table
    and column exists, and source IDs are matched by an exact identifier inside
    the persisted JSON snapshot (IDs are UUID-like and globally unique).
    """
    if not _learning_table_exists(conn, "document_chunks"):
        return
    _ensure_learning_lifecycle_columns(conn)
    revision_cols = _learning_columns(conn, "recall_card_revisions")
    card_cols = _learning_columns(conn, "recall_cards")
    if "id" not in revision_cols or "card_id" not in revision_cols or "source_citations_json" not in revision_cols:
        return
    if "id" not in card_cols:
        return

    card_assignments = []
    # is_paused is deliberately the only queue-gate write emitted from worker
    # triggers.  A legacy status/publication CHECK constraint must never cause a
    # parser/reparse UPDATE to fail.  source_status is an additive compatibility
    # marker and is safe to use when present.
    if "is_paused" in card_cols:
        card_assignments.append('"is_paused" = 1')
    if "source_status" in card_cols:
        card_assignments.append('"source_status" = \'invalid\'')
    if "updated_at" in card_cols:
        card_assignments.append('"updated_at" = CURRENT_TIMESTAMP')
    if not card_assignments:
        return

    progress_tables = []
    for table in ("learning_progress", "learning_day_units"):
        cols = _learning_columns(conn, table)
        if "card_id" not in cols:
            continue
        assignments = []
        if "state" in cols:
            assignments.append('"state" = \'paused\'')
        if "status" in cols:
            assignments.append('"status" = \'paused\'')
        if "updated_at" in cols:
            assignments.append('"updated_at" = CURRENT_TIMESTAMP')
        if assignments:
            progress_tables.append((table, assignments))

    revision_set = []
    # status/review_status vocabularies differ between preview schemas.  Use
    # source_status when available and otherwise fall back to review_status;
    # never force an unsupported status value from a worker trigger.
    if "source_status" in revision_cols:
        revision_set.append('"source_status" = \'invalid\'')
    elif "review_status" in revision_cols:
        revision_set.append('"review_status" = \'invalid\'')
    elif "status" in revision_cols:
        revision_set.append('"status" = \'invalid\'')
    if "updated_at" in revision_cols:
        revision_set.append('"updated_at" = CURRENT_TIMESTAMP')
    if not revision_set:
        return

    batch_set = None
    if _learning_table_exists(conn, "recall_generation_batches"):
        cols = _learning_columns(conn, "recall_generation_batches")
        assignments = []
        if "source_status" in cols:
            assignments.append('"source_status" = \'invalid\'')
        if "updated_at" in cols:
            assignments.append('"updated_at" = CURRENT_TIMESTAMP')
        if assignments:
            batch_set = assignments

    candidate_set = None
    if _learning_table_exists(conn, "extraction_candidates"):
        cols = _learning_columns(conn, "extraction_candidates")
        assignments = []
        if "source_status" in cols:
            assignments.append('"source_status" = \'invalid\'')
        if "status" in cols:
            # extraction candidate status already admits source-invalid states
            # in current schema; use it only after source_status when present.
            assignments.append('"status" = \'invalid\'')
        if assignments:
            candidate_set = assignments

    def _create(trigger_name: str, timing: str, operation: str, condition: str) -> None:
        existing = conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (trigger_name,)).fetchone()
        if existing:
            return
        statements = [
            f"DROP TRIGGER IF EXISTS \"{trigger_name}\"",
            f"CREATE TRIGGER IF NOT EXISTS \"{trigger_name}\" {timing} {operation} ON document_chunks WHEN {condition} BEGIN",
            f"  UPDATE recall_card_revisions SET {', '.join(revision_set)} WHERE source_citations_json LIKE '%' || OLD.id || '%'",
            f"  UPDATE recall_cards SET {', '.join(card_assignments)} WHERE id IN (SELECT card_id FROM recall_card_revisions WHERE source_citations_json LIKE '%' || OLD.id || '%')",
        ]
        for table, assignments in progress_tables:
            statements.append(
                f"  UPDATE \"{table}\" SET {', '.join(assignments)} "
                f"WHERE card_id IN (SELECT card_id FROM recall_card_revisions WHERE source_citations_json LIKE '%' || OLD.id || '%')"
            )
        if batch_set:
            statements.append(
                f"  UPDATE recall_generation_batches SET {', '.join(batch_set)} "
                "WHERE source_doc_id = OLD.doc_id"
            )
        if candidate_set:
            statements.append(
                f"  UPDATE extraction_candidates SET {', '.join(candidate_set)} "
                "WHERE citations_json LIKE '%' || OLD.id || '%'"
            )
        # The CREATE TRIGGER body must not place a semicolon directly after
        # BEGIN; terminate each body statement instead and close with END.
        drop_sql = statements[0] + ";"
        create_sql = statements[1] + "\n" + ";\n".join(statements[2:]) + ";\nEND;"
        conn.executescript(drop_sql + "\n" + create_sql)

    # Reparse keeps a chunk ID when location is unchanged; only actual source
    # text/error changes invalidate its citations.
    _create(
        "phase_f_chunk_source_changed",
        "AFTER",
        "UPDATE OF content, parse_error, page_number, paragraph_ref, heading, doc_id, chunk_index",
        "OLD.content IS NOT NEW.content OR OLD.parse_error IS NOT NEW.parse_error "
        "OR OLD.page_number IS NOT NEW.page_number OR OLD.paragraph_ref IS NOT NEW.paragraph_ref "
        "OR OLD.heading IS NOT NEW.heading OR OLD.doc_id IS NOT NEW.doc_id "
        "OR OLD.chunk_index IS NOT NEW.chunk_index",
    )
    _create("phase_f_chunk_source_deleted", "AFTER", "DELETE", "1")


def _learning_row_dict(row: Any) -> Dict[str, Any]:
    try:
        return dict(row)
    except Exception:
        return {}


def _normalise_ref_key(key: Any) -> str:
    return str(key or "").strip().lower().replace("-", "_")


def _json_source_matches(
    value: Any, chunk_ids: set[str], doc_ids: set[str]
) -> bool:
    """Match citation/source JSON without relying on SQLite's JSON1 extension."""
    if value is None:
        return False
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            # Plain source IDs occasionally appear in early draft rows.
            return value in chunk_ids or value in doc_ids
    if isinstance(value, dict):
        for key, child in value.items():
            normal = _normalise_ref_key(key)
            if isinstance(child, (str, int)):
                child_text = str(child)
                if (
                    normal in {"chunk_id", "source_chunk_id", "document_chunk_id", "chunkid"}
                    or normal.endswith("_chunk_id")
                ) and child_text in chunk_ids:
                    return True
                if (
                    normal in {"doc_id", "source_doc_id", "document_id", "documentid"}
                    or normal.endswith("_doc_id")
                ) and child_text in doc_ids:
                    return True
            if _json_source_matches(child, chunk_ids, doc_ids):
                return True
        return False
    if isinstance(value, (list, tuple, set)):
        return any(_json_source_matches(item, chunk_ids, doc_ids) for item in value)
    return False


def _row_source_matches(
    row: Dict[str, Any], chunk_ids: set[str], doc_ids: set[str]
) -> bool:
    """Match direct source columns and JSON citations in a learning row."""
    if not row:
        return False
    for key, value in row.items():
        normal = _normalise_ref_key(key)
        if value is None:
            continue
        text = str(value)
        if (
            normal in {"chunk_id", "source_chunk_id", "document_chunk_id", "chunkid"}
            or normal.endswith("_chunk_id")
        ) and text in chunk_ids:
            return True
        if (
            normal in {"doc_id", "source_doc_id", "document_id", "documentid"}
            or normal.endswith("_doc_id")
        ) and text in doc_ids:
            return True
        if normal.endswith("_json") or normal in {
            "citations",
            "source_citations",
            "metadata",
            "source_ids",
        }:
            if _json_source_matches(value, chunk_ids, doc_ids):
                return True
    return False


def _learning_rows(conn: sqlite3.Connection, table: str) -> List[Dict[str, Any]]:
    if not _learning_table_exists(conn, table):
        return []
    try:
        return [_learning_row_dict(row) for row in conn.execute(f'SELECT * FROM "{table}"').fetchall()]
    except Exception:
        return []


def _safe_update_learning_row(
    conn: sqlite3.Connection,
    table: str,
    row_id: Any,
    assignments: Dict[str, Any],
    *,
    id_column: str = "id",
) -> bool:
    """Best-effort update for one optional learning row."""
    columns = _learning_columns(conn, table)
    if not columns or id_column not in columns:
        return False
    usable = {key: value for key, value in assignments.items() if key in columns}
    if not usable:
        return False
    names = list(usable)
    sql = (
        f'UPDATE "{table}" SET '
        + ", ".join(f'"{name}" = ?' for name in names)
        + f' WHERE "{id_column}" = ?'
    )
    try:
        conn.execute(sql, [usable[name] for name in names] + [row_id])
        return True
    except (sqlite3.IntegrityError, sqlite3.OperationalError):
        # A status CHECK constraint or a legacy NOT NULL FK should not break
        # deletion/editing of the underlying material.  Retry each assignment
        # independently so source_status/is_paused can still be persisted when
        # a legacy status vocabulary rejects the other assignment.
        changed = False
        for name, value in usable.items():
            try:
                conn.execute(
                    f'UPDATE "{table}" SET "{name}" = ? WHERE "{id_column}" = ?',
                    (value, row_id),
                )
                changed = True
            except (sqlite3.IntegrityError, sqlite3.OperationalError):
                continue
        return changed


def _set_learning_status(
    conn: sqlite3.Connection,
    table: str,
    row_id: Any,
    *,
    source_status: Optional[str] = None,
    status: Optional[str] = None,
    updated_at: bool = True,
) -> None:
    assignments: Dict[str, Any] = {}
    columns = _learning_columns(conn, table)
    if source_status is not None and "source_status" in columns:
        assignments["source_status"] = source_status
    if status is not None and "status" in columns:
        assignments["status"] = status
    if updated_at and "updated_at" in columns:
        assignments["updated_at"] = datetime.utcnow().isoformat(sep=" ")
    if not assignments:
        return
    _safe_update_learning_row(conn, table, row_id, assignments)


def _mark_learning_cards_paused(
    conn: sqlite3.Connection,
    card_ids: set[str],
    *,
    source_status: Optional[str] = None,
) -> None:
    if not card_ids or not _learning_table_exists(conn, "recall_cards"):
        return
    _ensure_learning_lifecycle_columns(conn)
    card_columns = _learning_columns(conn, "recall_cards")
    for card_id in card_ids:
        assignments: Dict[str, Any] = {}
        # is_paused is the canonical queue gate in the Phase B/C services.  It
        # preserves the card's publication/study lifecycle and is therefore
        # preferable to overwriting status with a value a legacy CHECK may reject.
        if "is_paused" in card_columns:
            assignments["is_paused"] = 1
        # Keep both queue gate and lifecycle status in sync when the newer
        # publication_status column exists; study status remains untouched.
        if "publication_status" in card_columns:
            assignments["publication_status"] = "paused"
        elif "status" not in card_columns and "is_paused" not in card_columns:
            assignments["status"] = "paused"
        if source_status is not None and "source_status" in card_columns:
            assignments["source_status"] = source_status
        if "updated_at" in card_columns:
            assignments["updated_at"] = datetime.utcnow().isoformat(sep=" ")
        _safe_update_learning_row(conn, "recall_cards", card_id, assignments)

    # Progress, attempts, events, and day units are historical records.  The
    # card-level queue gate above blocks stale cards without rewriting that
    # history or resetting the learner's schedule.


def _mark_recall_sources_invalid(
    conn: sqlite3.Connection,
    *,
    chunk_ids: Optional[set[str]] = None,
    doc_ids: Optional[set[str]] = None,
    missing: bool = False,
) -> set[str]:
    """Invalidate recall citations/candidates for changed or removed sources.

    Returns impacted card IDs so callers can perform additional association
    updates.  Rows are never deleted.  ``source_status`` is additive where
    possible; status columns are also moved to an invalid value as a fallback
    so old learning readers that predate the column stop serving stale data.
    """
    chunk_ids = {str(value) for value in (chunk_ids or set()) if value is not None}
    doc_ids = {str(value) for value in (doc_ids or set()) if value is not None}
    if not chunk_ids and not doc_ids:
        return set()

    _ensure_learning_lifecycle_columns(conn)
    source_state = "missing" if missing else "invalid"

    chapter_ids: set[str] = set()
    batch_ids: set[str] = set()
    card_ids: set[str] = set()
    revision_ids: set[str] = set()

    # Chapters and generation batches can be linked to the source document
    # directly or through a JSON list of source documents.
    for row in _learning_rows(conn, "recall_chapters"):
        if _row_source_matches(row, chunk_ids, doc_ids):
            if row.get("id") is not None:
                chapter_ids.add(str(row["id"]))
            _set_learning_status(
                conn, "recall_chapters", row.get("id"), source_status=source_state,
                status="source_missing" if missing else None,
            )

    for row in _learning_rows(conn, "recall_generation_batches"):
        linked_chapter = row.get("chapter_id")
        if (
            _row_source_matches(row, chunk_ids, doc_ids)
            or (linked_chapter is not None and str(linked_chapter) in chapter_ids)
        ):
            if row.get("id") is not None:
                batch_ids.add(str(row["id"]))
            _set_learning_status(
                conn, "recall_generation_batches", row.get("id"),
                source_status=source_state, status="invalid",
            )

    # Cards can be linked to a document/chunk, a chapter, or a generation
    # batch.  We collect IDs before changing any rows so all revision/progress
    # links remain stable.
    for row in _learning_rows(conn, "recall_cards"):
        chapter_id = row.get("chapter_id")
        generation_batch_id = row.get("generation_batch_id")
        if (
            _row_source_matches(row, chunk_ids, doc_ids)
            or (chapter_id is not None and str(chapter_id) in chapter_ids)
            or (
                generation_batch_id is not None
                and str(generation_batch_id) in batch_ids
            )
        ) and row.get("id") is not None:
            card_ids.add(str(row["id"]))

    # Revisions are citation snapshots.  A revision is impacted either by its
    # own direct source fields, its citation JSON, or its owning card.
    revision_rows = _learning_rows(conn, "recall_card_revisions")
    for row in revision_rows:
        card_id = row.get("card_id")
        if (
            _row_source_matches(row, chunk_ids, doc_ids)
            or (card_id is not None and str(card_id) in card_ids)
        ) and row.get("id") is not None:
            revision_ids.add(str(row["id"]))
            if card_id is not None:
                card_ids.add(str(card_id))

    # If a revision pointed to a card, pause that card even when the card has
    # no source_doc_id of its own.  This also covers citation-only snapshots.
    _mark_learning_cards_paused(conn, card_ids, source_status=source_state)

    for revision_id in revision_ids:
        _set_learning_status(
            conn, "recall_card_revisions", revision_id,
            source_status=source_state, status="invalid",
        )

    # Extraction candidates retain their original row and become explicitly
    # invalid.  Their extraction batch is invalidated through either source
    # linkage or candidate membership.
    candidate_rows = _learning_rows(conn, "extraction_candidates")
    candidate_ids: set[str] = set()
    for row in candidate_rows:
        extraction_id = row.get("extraction_id")
        if (
            _row_source_matches(row, chunk_ids, doc_ids)
            or (extraction_id is not None and str(extraction_id) in batch_ids)
        ) and row.get("id") is not None:
            candidate_ids.add(str(row["id"]))
    for candidate_id in candidate_ids:
        _set_learning_status(
            conn, "extraction_candidates", candidate_id,
            source_status=source_state, status="invalid",
        )

    # A batch may only mention source chunks in its candidates; invalidate it
    # when any impacted candidate belongs to it, even if its own doc_ids_json
    # was absent in an early schema.
    for row in _learning_rows(conn, "extraction_batches"):
        batch_id = row.get("id")
        if (
            _row_source_matches(row, chunk_ids, doc_ids)
            or (batch_id is not None and str(batch_id) in batch_ids)
            or any(
                str(row.get("id")) == str(candidate.get("extraction_id"))
                for candidate in candidate_rows
                if candidate.get("id") is not None
                and str(candidate["id"]) in candidate_ids
            )
        ):
            if batch_id is not None:
                batch_ids.add(str(batch_id))
            _set_learning_status(
                conn, "extraction_batches", batch_id,
                source_status=source_state, status="invalid",
            )

    return card_ids


def _detach_recall_document_fks(conn: sqlite3.Connection, doc_id: str) -> None:
    """Null optional recall document FKs before hard-deleting a document."""
    for table in (
        "recall_chapters",
        "recall_generation_batches",
        "recall_cards",
        "recall_card_revisions",
        "extraction_candidates",
        "extraction_batches",
    ):
        columns = _learning_columns(conn, table)
        if not columns:
            continue
        for column in ("source_doc_id", "doc_id", "document_id"):
            if column not in columns:
                continue
            try:
                conn.execute(
                    f"UPDATE \\\"{table}\\\" SET \\\"{column}\\\" = NULL WHERE \\\"{column}\\\" = ?",
                    (doc_id,),
                )
            except (sqlite3.IntegrityError, sqlite3.OperationalError):
                continue


def _doc_chunk_ids(conn: sqlite3.Connection, doc_id: str) -> set[str]:
    try:
        return {
            str(row["id"])
            for row in conn.execute(
                "SELECT id FROM document_chunks WHERE doc_id = ?", (doc_id,)
            ).fetchall()
        }
    except Exception:
        return set()


def _doc_row_to_response(r: sqlite3.Row) -> DocumentResponse:
    d = dict(r)
    d["created_at"] = str(d["created_at"])
    d.setdefault("task_id", None)
    return DocumentResponse(**d)

def _chunk_row_to_response(r: sqlite3.Row) -> DocumentChunkResponse:
    d = dict(r)
    d["created_at"] = str(d["created_at"])
    return DocumentChunkResponse(**d)

def _parse_iso_to_dt(iso_str: Optional[str]) -> Optional[datetime]:
    if not iso_str:
        return None
    try:
        cleaned = iso_str.replace("Z", "").replace("T", " ")
        return datetime.fromisoformat(cleaned)
    except Exception:
        return None

def _task_row_to_response(r: Any, queue_positions: Optional[Dict[str, int]] = None) -> TaskResponse:
    d = dict(r)
    task_id = d["id"]
    status = d.get("status")

    # In-memory detail and timing priority, fallback to DB JSON
    detail_data = TaskWorker._task_details.get(task_id)
    if not detail_data and d.get("progress_detail_json"):
        try:
            detail_data = json.loads(d["progress_detail_json"])
        except Exception:
            detail_data = None

    timing_data = TaskWorker._task_timings.get(task_id)
    if not timing_data and d.get("timing_breakdown_json"):
        try:
            timing_data = json.loads(d["timing_breakdown_json"])
        except Exception:
            timing_data = None

    created_at_dt = _parse_iso_to_dt(str(d.get("created_at")))
    started_at_str = d.get("started_at")
    started_at_dt = _parse_iso_to_dt(started_at_str)
    updated_at_dt = _parse_iso_to_dt(str(d.get("updated_at")))
    now_dt = datetime.utcnow()

    elapsed_seconds = None
    queued_seconds = None

    if status == "pending":
        if created_at_dt:
            queued_seconds = round(max(0.0, (now_dt - created_at_dt).total_seconds()), 1)
    elif status in ("processing", "cancel_requested"):
        ref_start = started_at_dt or created_at_dt
        if ref_start:
            elapsed_seconds = round(max(0.0, (now_dt - ref_start).total_seconds()), 1)
        if created_at_dt and started_at_dt:
            queued_seconds = round(max(0.0, (started_at_dt - created_at_dt).total_seconds()), 1)
    elif status in ("completed", "partial", "failed", "cancelled"):
        ref_start = started_at_dt or created_at_dt
        ref_end = updated_at_dt or now_dt
        if ref_start and ref_end:
            elapsed_seconds = round(max(0.0, (ref_end - ref_start).total_seconds()), 1)
        if created_at_dt and started_at_dt:
            queued_seconds = round(max(0.0, (started_at_dt - created_at_dt).total_seconds()), 1)

    queue_position = None
    if status == "pending":
        if queue_positions and task_id in queue_positions:
            queue_position = queue_positions[task_id]
        else:
            try:
                with db_session() as conn:
                    pos = conn.execute("""
                        SELECT COUNT(*) FROM tasks
                        WHERE status = 'pending' AND hidden_at IS NULL AND created_at <= ?
                    """, (d.get("created_at"),)).fetchone()[0]
                    queue_position = pos
            except Exception:
                queue_position = 1

    progress_detail = None
    if detail_data:
        try:
            progress_detail = TaskProgressDetail(**detail_data)
        except Exception:
            pass

    timing_breakdown = None
    if timing_data:
        try:
            timing_cats = {}
            for k, v in timing_data.items():
                if isinstance(v, dict):
                    timing_cats[k] = TimingCategory(**v)
            timing_breakdown = TaskTimingBreakdown(**timing_cats)
        except Exception:
            pass

    return TaskResponse(
        id=d["id"],
        task_type=d["task_type"],
        target_id=d.get("target_id"),
        status=d["status"],
        progress=d["progress"],
        message=d.get("message"),
        error_msg=d.get("error_msg"),
        created_at=str(d["created_at"]),
        updated_at=str(d["updated_at"]),
        batch_errors_json=d.get("batch_errors_json"),
        retry_of=d.get("retry_of"),
        hidden_at=str(d["hidden_at"]) if d.get("hidden_at") else None,
        parent_task_id=d.get("parent_task_id"),
        started_at=str(d["started_at"]) if d.get("started_at") else None,
        elapsed_seconds=elapsed_seconds,
        queued_seconds=queued_seconds,
        queue_position=queue_position,
        progress_detail=progress_detail,
        timing_breakdown=timing_breakdown
    )

@router.post("/upload", response_model=DocumentResponse)
async def upload_material(
    file: UploadFile = File(...),
    file_type: str = Form(..., description="'textbook', 'exam', 'answer', 'supplement'"),
    year: Optional[int] = Form(None),
    session_name: Optional[str] = Form(None),
):
    valid_types = ["textbook", "exam", "answer", "exam_and_answer", "supplement"]
    if file_type not in valid_types:
        raise HTTPException(status_code=400, detail=f"文件类型无效，必须是 {valid_types} 之一")

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in [".pdf", ".docx", ".jpg", ".jpeg", ".png"]:
        raise HTTPException(status_code=400, detail="仅支持 PDF, DOCX, JPG, PNG 格式文件")

    # Stream to a temporary file while hashing, so a 100 MB upload does not
    # occupy another 100 MB Python buffer.
    hasher = hashlib.sha256()
    total_size = 0
    temp_path = settings.uploads_path / f".{uuid.uuid4().hex}.upload"
    try:
        with temp_path.open("wb") as temp_file:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total_size += len(chunk)
                if total_size > 100 * 1024 * 1024:
                    raise HTTPException(status_code=400, detail="文件大小超过 100MB 限制")
                hasher.update(chunk)
                temp_file.write(chunk)
    except HTTPException:
        temp_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        temp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"上传文件读取失败: {type(exc).__name__}") from exc

    if total_size == 0:
        temp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="不能导入空文件")

    file_hash = hasher.hexdigest()

    # Step 1: Check existing or insert document in DB session
    existing_row = None
    new_doc_row = None
    doc_id = str(uuid.uuid4())

    with db_session() as conn:
        _ensure_learning_lifecycle_triggers(conn)
        cursor = conn.cursor()
        cursor.execute("""
        SELECT id, filename, file_hash, file_type, year, session_name, status, error_msg, page_count, created_at
        FROM documents 
        WHERE file_hash = ?
        """, (file_hash,))
        existing = cursor.fetchone()
        if existing:
            existing_row = dict(existing)
            cursor.execute("""
                SELECT id FROM tasks WHERE task_type = 'parse_document' AND target_id = ?
                ORDER BY created_at DESC LIMIT 1
            """, (existing_row["id"],))
            task_row = cursor.fetchone()
            existing_row["task_id"] = task_row["id"] if task_row else None
        else:
            # Save file to disk
            save_path = settings.uploads_path / f"{file_hash}{suffix}"
            temp_path.replace(save_path)

            cursor.execute("""
            INSERT INTO documents (id, filename, file_hash, file_type, year, session_name, status, page_count)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', 0)
            """, (doc_id, file.filename, file_hash, file_type, year, session_name))

            cursor.execute("""
            SELECT id, filename, file_hash, file_type, year, session_name, status, error_msg, page_count, created_at
            FROM documents 
            WHERE id = ?
            """, (doc_id,))
            new_doc_row = cursor.fetchone()

            task_id = str(uuid.uuid4())
            cursor.execute("""
            INSERT INTO tasks (id, task_type, target_id, status, progress, message)
            VALUES (?, 'parse_document', ?, 'pending', 0, ?)
            """, (task_id, doc_id, f"解析文档: {file.filename}"))
            new_doc_row = dict(new_doc_row)
            new_doc_row["task_id"] = task_id

    # If already existed, return existing document and remove the streamed temp.
    if existing_row:
        temp_path.unlink(missing_ok=True)
        return _doc_row_to_response(existing_row)

    return _doc_row_to_response(new_doc_row)

@router.get("", response_model=List[DocumentResponse])
def list_materials(file_type: Optional[str] = None):
    with db_session() as conn:
        _ensure_learning_lifecycle_triggers(conn)
        cursor = conn.cursor()
        if file_type:
            cursor.execute("SELECT * FROM documents WHERE file_type = ? ORDER BY created_at DESC", (file_type,))
        else:
            cursor.execute("SELECT * FROM documents ORDER BY created_at DESC")
        rows = cursor.fetchall()
        return [_doc_row_to_response(r) for r in rows]

@router.get("/{doc_id}/chunks", response_model=List[DocumentChunkResponse])
def get_document_chunks(doc_id: str):
    with db_session() as conn:
        cursor = conn.cursor()
        if not cursor.execute("SELECT 1 FROM documents WHERE id = ?", (doc_id,)).fetchone():
            raise HTTPException(status_code=404, detail="文档未找到")
        cursor.execute("""
        SELECT id, doc_id, chunk_index, page_number, heading, paragraph_ref, content,
               original_content, parse_error, embedding_status, created_at
        FROM document_chunks
        WHERE doc_id = ?
        ORDER BY chunk_index ASC
        """, (doc_id,))
        rows = cursor.fetchall()
        return [_chunk_row_to_response(r) for r in rows]


@router.get("/{doc_id}/file")
def get_document_file(doc_id: str):
    with db_session() as conn:
        row = conn.execute(
            "SELECT filename, file_hash FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="文档未找到")
    suffix = Path(row["filename"] or "").suffix.lower()
    file_path = settings.uploads_path / f"{row['file_hash']}{suffix}"
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="原始文件不存在")
    return FileResponse(file_path, filename=row["filename"])

@router.put("/chunks/{chunk_id}", response_model=DocumentChunkResponse)
def update_chunk_content(chunk_id: str, req: ChunkUpdateRequest):
    """Allow manual proofreading and text modification."""
    from app.services.parser.text_splitter import segment_chinese
    content = req.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="原文内容不能为空")
    new_segmented = segment_chinese(content)

    with db_session() as conn:
        _ensure_learning_lifecycle_triggers(conn)
        cursor = conn.cursor()
        cursor.execute("SELECT id, doc_id, content FROM document_chunks WHERE id = ?", (chunk_id,))
        chunk_row = cursor.fetchone()
        if not chunk_row:
            raise HTTPException(status_code=404, detail="片段不存在")

        # A manual edit changes the grounding snapshot.  Invalidate related
        # recall revisions/candidates before replacing the source while
        # retaining every card, progress row, attempt, and event.
        _mark_recall_sources_invalid(
            conn,
            chunk_ids={chunk_id},
            missing=False,
        )
        cursor.execute("""
        UPDATE document_chunks
        SET content = ?, content_segmented = ?, parse_error = NULL,
            embedding = NULL, embedding_status = 'pending', updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """, (content, new_segmented, chunk_id))

        # Update FTS5 table
        cursor.execute("DELETE FROM document_chunks_fts WHERE chunk_id = ?", (chunk_id,))
        cursor.execute("INSERT INTO document_chunks_fts (chunk_id, content_segmented) VALUES (?, ?)", (chunk_id, new_segmented))

        cursor.execute("""
            UPDATE questions
            SET needs_review = 1, is_confirmed = 0, review_reason = '原文切片已校对，需重新确认'
            WHERE source_chunk_id = ? OR answer_source_chunk_id = ?
        """, (chunk_id, chunk_id))
        cursor.execute("""
            UPDATE study_cards SET needs_update = 1
            WHERE knowledge_point_id IN (
                SELECT rel.knowledge_point_id
                FROM question_knowledge_rel rel
                JOIN questions q ON q.id = rel.question_id
                WHERE q.source_chunk_id = ? OR q.answer_source_chunk_id = ?
            )
        """, (chunk_id, chunk_id))

        cursor.execute("SELECT * FROM document_chunks WHERE id = ?", (chunk_id,))
        row = cursor.fetchone()

    TaskWorker.create_task("reindex_chunk", chunk_id, "原文已校对，等待更新语义向量")
    return _chunk_row_to_response(row)

@router.delete("/{doc_id}")
def delete_document(doc_id: str):
    with db_session() as conn:
        _ensure_learning_lifecycle_triggers(conn)
        cursor = conn.cursor()
        cursor.execute("SELECT file_hash FROM documents WHERE id = ?", (doc_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="文档未找到")

        # Hard-delete is intentionally retained for the material store, but
        # learning rows must become source-missing first.  Capture chunk IDs
        # before the document's ON DELETE CASCADE removes them.
        chunk_ids = _doc_chunk_ids(conn, doc_id)
        _mark_recall_sources_invalid(
            conn,
            chunk_ids=chunk_ids,
            doc_ids={doc_id},
            missing=True,
        )
        # Null optional document FKs so installations with stricter/older FK
        # definitions cannot cascade away recall history on document delete.
        _detach_recall_document_fks(conn, doc_id)

        file_hash = row["file_hash"]
        # Mark queued work as cancelled before removing its target.
        conn.execute("""
            UPDATE tasks SET status = 'failed', error_msg = '文档已删除', message = '文档已删除', updated_at = CURRENT_TIMESTAMP
            WHERE target_id = ? AND status IN ('pending', 'processing')
        """, (doc_id,))
        conn.execute("""
            UPDATE study_cards SET needs_update = 1
            WHERE knowledge_point_id IN (
                SELECT rel.knowledge_point_id
                FROM question_knowledge_rel rel
                JOIN questions q ON q.id = rel.question_id
                WHERE q.doc_id = ?
                   OR q.source_chunk_id IN (SELECT id FROM document_chunks WHERE doc_id = ?)
                   OR q.answer_source_chunk_id IN (SELECT id FROM document_chunks WHERE doc_id = ?)
            )
        """, (doc_id, doc_id, doc_id))
        conn.execute("""
            UPDATE questions
            SET doc_id = CASE WHEN doc_id = ? THEN NULL ELSE doc_id END,
                source_chunk_id = CASE
                    WHEN source_chunk_id IN (SELECT id FROM document_chunks WHERE doc_id = ?) THEN NULL
                    ELSE source_chunk_id END,
                answer_source_doc_id = CASE WHEN answer_source_chunk_id IN (SELECT id FROM document_chunks WHERE doc_id = ?) THEN NULL ELSE answer_source_doc_id END,
                answer_source_chunk_id = CASE
                    WHEN answer_source_chunk_id IN (SELECT id FROM document_chunks WHERE doc_id = ?) THEN NULL
                    ELSE answer_source_chunk_id END,
                is_confirmed = 0, needs_review = 1, review_reason = '原始文档已删除，需重新核对'
            WHERE doc_id = ?
               OR source_chunk_id IN (SELECT id FROM document_chunks WHERE doc_id = ?)
               OR answer_source_chunk_id IN (SELECT id FROM document_chunks WHERE doc_id = ?)
        """, (doc_id, doc_id, doc_id, doc_id, doc_id, doc_id, doc_id))
        # Mark source knowledge points as needing supplement
        conn.execute("""
            UPDATE knowledge_points
            SET source_status = 'supplement_needed'
            WHERE id IN (
                SELECT rel.knowledge_point_id
                FROM question_knowledge_rel rel
                JOIN questions q ON q.id = rel.question_id
                WHERE q.doc_id = ?
            ) OR source_reference LIKE ?
        """, (doc_id, f"%{doc_id}%"))
        # Delete chunk FTS records
        cursor.execute("DELETE FROM document_chunks_fts WHERE chunk_id IN (SELECT id FROM document_chunks WHERE doc_id = ?)", (doc_id,))
        cursor.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    for ext in (".pdf", ".docx", ".jpg", ".jpeg", ".png"):
        (settings.uploads_path / f"{file_hash}{ext}").unlink(missing_ok=True)
    return {"status": "ok", "message": "文档、原始文件和切片已删除"}

# Task endpoints
@router.get("/tasks/active/list", response_model=List[TaskResponse])
def get_active_tasks():
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM tasks
            WHERE status IN ('pending', 'processing', 'cancel_requested') AND hidden_at IS NULL
            ORDER BY created_at DESC LIMIT 20
        """)
        rows = cursor.fetchall()
        return [_task_row_to_response(r) for r in rows]

@router.get("/tasks/history", response_model=TaskHistoryResponse)
def get_task_history(
    category: Optional[str] = None, # 'running', 'actionable', 'history', 'all'
    status: Optional[str] = None,
    page: int = 1,
    limit: int = 50
):
    page = max(1, page)
    limit = max(1, min(limit, 200))
    offset = (page - 1) * limit

    with db_session() as conn:
        cursor = conn.cursor()
        active_cnt = cursor.execute("""
            SELECT COUNT(*) FROM tasks
            WHERE status IN ('pending', 'processing', 'cancel_requested') AND hidden_at IS NULL
        """).fetchone()[0]

        actionable_cnt = cursor.execute("""
            SELECT COUNT(*) FROM tasks
            WHERE status IN ('failed', 'partial') AND hidden_at IS NULL
        """).fetchone()[0]

        history_cnt = cursor.execute("""
            SELECT COUNT(*) FROM tasks
            WHERE status IN ('completed', 'cancelled', 'failed', 'partial') AND hidden_at IS NULL
        """).fetchone()[0]

        query = "SELECT * FROM tasks WHERE hidden_at IS NULL"
        params = []

        if status:
            query += " AND status = ?"
            params.append(status)
        elif category == "running":
            query += " AND status IN ('pending', 'processing', 'cancel_requested')"
        elif category == "actionable":
            query += " AND status IN ('failed', 'partial')"
        elif category == "history":
            query += " AND status IN ('completed', 'cancelled', 'failed', 'partial')"

        count_query = query.replace("SELECT *", "SELECT COUNT(*)", 1)
        total = cursor.execute(count_query, tuple(params)).fetchone()[0]

        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = cursor.execute(query, tuple(params)).fetchall()

        return TaskHistoryResponse(
            tasks=[_task_row_to_response(r) for r in rows],
            total=total,
            page=page,
            limit=limit,
            active_count=active_cnt,
            actionable_count=actionable_cnt,
            history_count=history_cnt
        )

@router.get("/tasks/list", response_model=List[TaskResponse])
def get_task_history_legacy(limit: int = 50):
    limit = max(1, min(limit, 200))
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tasks WHERE hidden_at IS NULL ORDER BY created_at DESC LIMIT ?", (limit,))
        return [_task_row_to_response(r) for r in cursor.fetchall()]

@router.post("/tasks/{task_id}/cancel", response_model=TaskResponse)
def cancel_task(task_id: str):
    res = TaskWorker.request_cancel(task_id)
    if not res:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _task_row_to_response(res)

@router.delete("/tasks/{task_id}")
def delete_task_record(task_id: str):
    with db_session() as conn:
        cursor = conn.cursor()
        task = cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        if task["status"] in ("pending", "processing", "cancel_requested"):
            raise HTTPException(status_code=400, detail="进行中的任务不能直接清理，请先取消任务")
        cursor.execute("UPDATE tasks SET hidden_at = CURRENT_TIMESTAMP WHERE id = ?", (task_id,))
    return {"status": "ok", "message": "任务记录已隐藏", "id": task_id}

@router.post("/tasks/clear", response_model=TaskClearResponse)
def clear_tasks(req: TaskClearRequest):
    clear_token = str(uuid.uuid4())
    with db_session() as conn:
        cursor = conn.cursor()
        if req.scope == "completed":
            cursor.execute("""
                UPDATE tasks
                SET hidden_at = CURRENT_TIMESTAMP, clear_token = ?
                WHERE status = 'completed' AND hidden_at IS NULL
            """, (clear_token,))
        else: # terminal
            cursor.execute("""
                UPDATE tasks
                SET hidden_at = CURRENT_TIMESTAMP, clear_token = ?
                WHERE status IN ('completed', 'cancelled', 'failed', 'partial') AND hidden_at IS NULL
            """, (clear_token,))
        cleared_cnt = cursor.rowcount
    return TaskClearResponse(cleared_count=cleared_cnt, clear_token=clear_token)

@router.post("/tasks/restore", response_model=TaskRestoreResponse)
def restore_cleared_tasks(req: TaskRestoreRequest):
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE tasks
            SET hidden_at = NULL, clear_token = NULL
            WHERE clear_token = ?
        """, (req.clear_token,))
        restored_cnt = cursor.rowcount
    return TaskRestoreResponse(restored_count=restored_cnt)

@router.post("/tasks/{task_id}/retry", response_model=TaskResponse)
def retry_task(task_id: str):
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="任务不存在")
        if row["status"] not in ("failed", "partial"):
            raise HTTPException(status_code=400, detail="只有失败或部分失败的任务可以重试")
        new_id = str(uuid.uuid4())
        if row["task_type"] == "parse_document":
            cursor.execute(
                "UPDATE documents SET status = 'pending', error_msg = NULL WHERE id = ?",
                (row["target_id"],),
            )
        cursor.execute("""
            INSERT INTO tasks (id, task_type, target_id, status, progress, message, retry_of)
            VALUES (?, ?, ?, 'pending', 0, ?, ?)
        """, (new_id, row["task_type"], row["target_id"], f"重试任务: {row['task_type']}", task_id))
        cursor.execute("SELECT * FROM tasks WHERE id = ?", (new_id,))
        return _task_row_to_response(cursor.fetchone())

@router.get("/tasks/{task_id}", response_model=TaskResponse)
def get_task_status(task_id: str):
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="任务不存在")
        return _task_row_to_response(row)

@router.get("/tasks/{task_id}/events", response_model=TaskEventsResponse)
def get_task_events(task_id: str, after_id: Optional[int] = None):
    with db_session() as conn:
        cursor = conn.cursor()
        task = cursor.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")

        if after_id is not None:
            rows = cursor.execute("""
                SELECT id, task_id, event_type, stage, unit_index, message, data_json, created_at
                FROM task_events
                WHERE task_id = ? AND id > ?
                ORDER BY id ASC
                LIMIT 200
            """, (task_id, after_id)).fetchall()
        else:
            rows = cursor.execute("""
                SELECT id, task_id, event_type, stage, unit_index, message, data_json, created_at
                FROM task_events
                WHERE task_id = ?
                ORDER BY id ASC
                LIMIT 200
            """, (task_id,)).fetchall()

        events = []
        last_id = after_id
        for r in rows:
            rd = dict(r)
            eid = rd["id"]
            if last_id is None or eid > last_id:
                last_id = eid
            data = None
            if rd.get("data_json"):
                try:
                    data = json.loads(rd["data_json"])
                except Exception:
                    data = None
            events.append(TaskEventItem(
                id=rd["id"],
                task_id=rd["task_id"],
                event_type=rd["event_type"],
                stage=rd.get("stage"),
                unit_index=rd.get("unit_index"),
                message=rd["message"],
                data=data,
                created_at=str(rd["created_at"])
            ))

        return TaskEventsResponse(events=events, last_id=last_id)
