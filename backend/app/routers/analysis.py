import uuid
import logging
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Query
from app.database import db_session
from app.models.schemas import (
    KnowledgePointCreate, KnowledgePointUpdate, KnowledgePointResponse,
    KnowledgePointMergeRequest, KnowledgePointStats, OverallStatsResponse,
    ExtractionCreateRequest, ExtractionBatchResponse, CandidateResponse,
    ApplyCandidatesRequest, ApplyCandidatesResponse,
    QuestionLinkSuggestionResponse, QuestionLinkApplyRequest, QuestionLinkApplyResponse,
    ExtractionBatchListResponse, CandidateSourceReview, CandidateSourceReviewRequest
)
from app.services.stats_engine import StatsEngine
from app.services.knowledge_extractor import KnowledgeExtractor
from app.routers.materials import (
    _ensure_learning_lifecycle_columns,
    _learning_columns,
    _learning_table_exists,
    _mark_learning_cards_paused,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/analysis", tags=["analysis"])


def _set_learning_kp_status(conn, kp_id: str, *, source_status=None, deleted=False) -> None:
    """Pause recall cards for a KP without deleting learning history."""
    if not _learning_table_exists(conn, "recall_cards"):
        return
    _ensure_learning_lifecycle_columns(conn)
    columns = _learning_columns(conn, "recall_cards")
    rows = conn.execute("SELECT * FROM recall_cards").fetchall()
    card_ids = {
        str(row["id"])
        for row in rows
        if row["id"] is not None and "knowledge_point_id" in columns
        and str(row["knowledge_point_id"]) == str(kp_id)
    }
    _mark_learning_cards_paused(conn, card_ids, source_status=source_status)


def _reassociate_learning_kp(conn, source_id: str, target_id: str) -> None:
    """Move cards to the merged KP while preserving card IDs/history."""
    if not _learning_table_exists(conn, "recall_cards"):
        return
    columns = _learning_columns(conn, "recall_cards")
    if "knowledge_point_id" not in columns:
        return
    # Do not merge cards: each source card remains a distinct stable card.
    conn.execute(
        "UPDATE recall_cards SET knowledge_point_id = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE knowledge_point_id = ?",
        (target_id, source_id),
    )


@router.get("/stats", response_model=OverallStatsResponse)
def get_overall_stats():
    return StatsEngine.get_overall_stats()

@router.get("/point-stats", response_model=List[KnowledgePointStats])
def get_knowledge_points_stats():
    return StatsEngine.get_knowledge_points_stats()

@router.get("/knowledge-points", response_model=List[KnowledgePointResponse])
def list_knowledge_points():
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM knowledge_points
            WHERE is_deleted = 0
            ORDER BY sort_order ASC, code ASC, name ASC
        """)
        rows = cursor.fetchall()
        return [
            KnowledgePointResponse(
                id=r["id"],
                code=r["code"],
                name=r["name"],
                chapter=r["chapter"],
                parent_id=r["parent_id"],
                priority_tag=r["priority_tag"] or "normal",
                sort_order=r["sort_order"] or 0,
                source_type=r["source_type"] or "pending_verification",
                source_status=r["source_status"] or "valid",
                source_reference=r["source_reference"],
                is_deleted=r["is_deleted"] or 0,
                deleted_at=str(r["deleted_at"]) if r["deleted_at"] else None,
                created_at=str(r["created_at"]),
                children=[]
            ) for r in rows
        ]

@router.get("/knowledge-points/legacy", response_model=List[KnowledgePointResponse])
def get_legacy_knowledge_points():
    """Returns knowledge points that have pending_verification source status and are not deleted."""
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM knowledge_points
            WHERE (source_type = 'pending_verification' OR source_type IS NULL)
              AND is_deleted = 0
            ORDER BY sort_order ASC, code ASC, name ASC
        """)
        rows = cursor.fetchall()
        return [
            KnowledgePointResponse(
                id=r["id"],
                code=r["code"],
                name=r["name"],
                chapter=r["chapter"],
                parent_id=r["parent_id"],
                priority_tag=r["priority_tag"] or "normal",
                sort_order=r["sort_order"] or 0,
                source_type=r["source_type"] or "pending_verification",
                source_status=r["source_status"] or "valid",
                source_reference=r["source_reference"],
                is_deleted=r["is_deleted"] or 0,
                deleted_at=str(r["deleted_at"]) if r["deleted_at"] else None,
                created_at=str(r["created_at"]),
                children=[]
            ) for r in rows
        ]

@router.post("/knowledge-points", response_model=KnowledgePointResponse)
def create_knowledge_point(req: KnowledgePointCreate):
    kpid = str(uuid.uuid4())
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("""
        INSERT INTO knowledge_points (
            id, code, name, chapter, parent_id, priority_tag, sort_order,
            source_type, source_status, source_reference, is_deleted
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """, (
            kpid, req.code, req.name, req.chapter, req.parent_id,
            req.priority_tag, req.sort_order,
            req.source_type or "manual",
            req.source_status or "valid",
            req.source_reference
        ))

        cursor.execute("SELECT * FROM knowledge_points WHERE id = ?", (kpid,))
        r = cursor.fetchone()
        return KnowledgePointResponse(
            id=r["id"],
            code=r["code"],
            name=r["name"],
            chapter=r["chapter"],
            parent_id=r["parent_id"],
            priority_tag=r["priority_tag"] or "normal",
            sort_order=r["sort_order"] or 0,
            source_type=r["source_type"] or "manual",
            source_status=r["source_status"] or "valid",
            source_reference=r["source_reference"],
            is_deleted=r["is_deleted"] or 0,
            deleted_at=str(r["deleted_at"]) if r["deleted_at"] else None,
            created_at=str(r["created_at"]),
            children=[]
        )

@router.put("/knowledge-points/{kp_id}", response_model=KnowledgePointResponse)
def update_knowledge_point(kp_id: str, req: KnowledgePointUpdate):
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM knowledge_points WHERE id = ?", (kp_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="知识点不存在")

        fields = []
        params = []
        if req.code is not None:
            fields.append("code = ?")
            params.append(req.code)
        if req.name is not None:
            fields.append("name = ?")
            params.append(req.name)
        if req.chapter is not None:
            fields.append("chapter = ?")
            params.append(req.chapter)
        if req.parent_id is not None:
            fields.append("parent_id = ?")
            params.append(req.parent_id)
        if req.priority_tag is not None:
            fields.append("priority_tag = ?")
            params.append(req.priority_tag)
        if req.sort_order is not None:
            fields.append("sort_order = ?")
            params.append(req.sort_order)
        if req.source_type is not None:
            fields.append("source_type = ?")
            params.append(req.source_type)
        if req.source_status is not None:
            fields.append("source_status = ?")
            params.append(req.source_status)
        if req.source_reference is not None:
            fields.append("source_reference = ?")
            params.append(req.source_reference)

        if fields:
            params.append(kp_id)
            cursor.execute(f"UPDATE knowledge_points SET {', '.join(fields)} WHERE id = ?", tuple(params))

        # Name/chapter/code edits do not touch recall progress.  Explicit
        # source-status edits do, however, pause cards when the source becomes
        # invalid; restoring a KP never clears that source invalidation.
        if req.source_status is not None and req.source_status != "valid":
            _set_learning_kp_status(conn, kp_id, source_status=req.source_status)

        cursor.execute("SELECT * FROM knowledge_points WHERE id = ?", (kp_id,))
        r = cursor.fetchone()
        return KnowledgePointResponse(
            id=r["id"],
            code=r["code"],
            name=r["name"],
            chapter=r["chapter"],
            parent_id=r["parent_id"],
            priority_tag=r["priority_tag"] or "normal",
            sort_order=r["sort_order"] or 0,
            source_type=r["source_type"] or "manual",
            source_status=r["source_status"] or "valid",
            source_reference=r["source_reference"],
            is_deleted=r["is_deleted"] or 0,
            deleted_at=str(r["deleted_at"]) if r["deleted_at"] else None,
            created_at=str(r["created_at"]),
            children=[]
        )

@router.post("/knowledge-points/merge")
def merge_knowledge_points(req: KnowledgePointMergeRequest):
    """
    Merges synonymous knowledge points:
    Reassigns question links from source_ids to target_id, reassigns practice sessions,
    and soft-deletes source_ids to preserve historical practice records.
    """
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name FROM knowledge_points WHERE id = ? AND is_deleted = 0", (req.target_id,))
        target_kp = cursor.fetchone()
        if not target_kp:
            raise HTTPException(status_code=404, detail="目标知识点不存在或已被删除")

        for src_id in req.source_ids:
            if src_id == req.target_id:
                continue

            # Fetch questions linked to source
            cursor.execute("SELECT question_id FROM question_knowledge_rel WHERE knowledge_point_id = ?", (src_id,))
            q_rows = cursor.fetchall()
            for qr in q_rows:
                qid = qr["question_id"]
                # Insert or ignore into target
                cursor.execute("""
                INSERT OR IGNORE INTO question_knowledge_rel (id, question_id, knowledge_point_id)
                VALUES (?, ?, ?)
                """, (str(uuid.uuid4()), qid, req.target_id))

            # Move recall-card KP association without deleting cards.  Card IDs,
            # revisions, progress and events remain stable; duplicate cards are
            # intentionally not merged and old question frequencies are untouched.
            _reassociate_learning_kp(conn, src_id, req.target_id)

            # Delete source links
            cursor.execute("DELETE FROM question_knowledge_rel WHERE knowledge_point_id = ?", (src_id,))
            cursor.execute("DELETE FROM study_cards WHERE knowledge_point_id = ?", (src_id,))

            # Reassign practice sessions to target_id so historical sessions are preserved!
            cursor.execute("UPDATE practice_sessions SET knowledge_point_id = ? WHERE knowledge_point_id = ?", (req.target_id, src_id))

            # Soft-delete source knowledge point
            cursor.execute("UPDATE knowledge_points SET is_deleted = 1, deleted_at = CURRENT_TIMESTAMP WHERE id = ?", (src_id,))

        # Mark target study card as needing update
        cursor.execute("UPDATE study_cards SET needs_update = 1 WHERE knowledge_point_id = ?", (req.target_id,))

    return {"status": "ok", "message": f"成功将 {len(req.source_ids)} 个同义知识点合并至目标知识点"}

@router.delete("/knowledge-points/{kp_id}")
def delete_knowledge_point(kp_id: str):
    """Soft deletes a knowledge point, keeping historical practice sessions intact."""
    with db_session() as conn:
        cursor = conn.cursor()
        row = cursor.execute("SELECT id, name FROM knowledge_points WHERE id = ? AND is_deleted = 0", (kp_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="知识点不存在或已被删除")

        cursor.execute("""
            UPDATE knowledge_points
            SET is_deleted = 1, deleted_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (kp_id,))
        # A deleted KP must stop appearing in recall queues, but all card and
        # learning history is retained for audit/restore.  Source invalidation
        # is distinct from deletion and is not cleared by restore.
        _set_learning_kp_status(conn, kp_id, source_status="invalid", deleted=True)
    return {"status": "ok", "message": "考点已成功删除", "id": kp_id}

@router.post("/knowledge-points/{kp_id}/restore")
def restore_knowledge_point(kp_id: str):
    """Restores a soft-deleted knowledge point."""
    with db_session() as conn:
        cursor = conn.cursor()
        row = cursor.execute("SELECT id, name FROM knowledge_points WHERE id = ?", (kp_id,))
        if not row.fetchone():
            raise HTTPException(status_code=404, detail="知识点不存在")

        cursor.execute("""
            UPDATE knowledge_points
            SET is_deleted = 0, deleted_at = NULL
            WHERE id = ?
        """, (kp_id,))
        # Restore only reverses KP visibility.  It must not override an
        # independent source-invalid status produced by an edited/deleted doc.
    return {"status": "ok", "message": "考点已成功恢复", "id": kp_id}


# --- Knowledge Point Extraction Endpoints ---

@router.post("/extractions", response_model=ExtractionBatchResponse)
def create_extraction(req: ExtractionCreateRequest):
    try:
        batch = KnowledgeExtractor.create_extraction_batch(req.doc_ids)
        return ExtractionBatchResponse(**batch)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Failed to create extraction batch: %s", str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"创建考点提取批次失败: {str(e)}")

@router.get("/extractions/latest", response_model=ExtractionBatchResponse)
def get_latest_extraction():
    batch = KnowledgeExtractor.get_latest_extraction()
    if not batch:
        raise HTTPException(status_code=404, detail="尚无考点提取批次")
    return ExtractionBatchResponse(**batch)

@router.get("/extractions", response_model=ExtractionBatchListResponse)
def list_extractions(
    pending_only: bool = Query(False),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    return KnowledgeExtractor.list_extractions(pending_only=pending_only, limit=limit, offset=offset)

@router.get("/extractions/{extraction_id}", response_model=ExtractionBatchResponse)
def get_extraction(extraction_id: str):
    batch = KnowledgeExtractor.get_extraction_batch(extraction_id)
    if not batch:
        raise HTTPException(status_code=404, detail="提取批次不存在")
    return ExtractionBatchResponse(**batch)

@router.get("/extractions/{extraction_id}/candidates", response_model=List[CandidateResponse])
def get_extraction_candidates(extraction_id: str):
    return KnowledgeExtractor.get_candidates(extraction_id)

@router.get("/extractions/{extraction_id}/candidates/{candidate_id}/source-review", response_model=CandidateSourceReview)
def get_candidate_source_review(extraction_id: str, candidate_id: str):
    review = KnowledgeExtractor.get_candidate_source_review(extraction_id, candidate_id)
    if not review:
        raise HTTPException(status_code=404, detail="候选考点不存在")
    return review

@router.post("/extractions/{extraction_id}/candidates/{candidate_id}/source-review", response_model=CandidateResponse)
def confirm_candidate_source_review(extraction_id: str, candidate_id: str, req: CandidateSourceReviewRequest):
    try:
        return KnowledgeExtractor.confirm_candidate_source_review(extraction_id, candidate_id, req)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

@router.post("/extractions/{extraction_id}/apply", response_model=ApplyCandidatesResponse)
def apply_extraction_candidates(extraction_id: str, req: ApplyCandidatesRequest):
    try:
        counts = KnowledgeExtractor.apply_candidates(extraction_id, req.decisions)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return ApplyCandidatesResponse(**counts)

@router.get("/extractions/{extraction_id}/question-links", response_model=QuestionLinkSuggestionResponse)
def get_question_link_suggestions(extraction_id: str):
    data = KnowledgeExtractor.get_question_links(extraction_id)
    return QuestionLinkSuggestionResponse(**data)

@router.post("/extractions/{extraction_id}/question-links/apply", response_model=QuestionLinkApplyResponse)
def apply_question_links(extraction_id: str, req: QuestionLinkApplyRequest):
    res = KnowledgeExtractor.apply_question_links(req.links)
    return QuestionLinkApplyResponse(**res)
