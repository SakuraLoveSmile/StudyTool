import uuid
import logging
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Query
from app.database import db_session
from app.models.schemas import AnswerCandidateResponse, QuestionCreate, QuestionUpdate, QuestionResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/questions", tags=["questions"])

def _format_question_row(row: dict, kp_map: dict) -> QuestionResponse:
    qid = row["id"]
    kps = kp_map.get(qid, [])
    kp_ids = [k["id"] for k in kps]
    kp_names = [k["name"] for k in kps]

    return QuestionResponse(
        id=row["id"],
        doc_id=row["doc_id"],
        year=row["year"],
        session_name=row["session_name"],
        question_num=row["question_num"],
        question_type=row["question_type"],
        score=row["score"],
        stem=row["stem"],
        standard_answer=row["standard_answer"],
        source_location=row["source_location"],
        source_chunk_id=row.get("source_chunk_id"),
        source_page=row.get("source_page"),
        source_paragraph_ref=row.get("source_paragraph_ref"),
        answer_source_chunk_id=row.get("answer_source_chunk_id"),
        answer_source_doc_id=row.get("answer_source_doc_id"),
        answer_source_page=row.get("answer_source_page"),
        answer_source_paragraph_ref=row.get("answer_source_paragraph_ref"),
        needs_review=row.get("needs_review", 1),
        review_reason=row.get("review_reason"),
        is_confirmed=row["is_confirmed"],
        is_ai_generated=row["is_ai_generated"],
        knowledge_point_ids=kp_ids,
        knowledge_point_names=kp_names,
        created_at=str(row["created_at"])
    )

@router.get("", response_model=List[QuestionResponse])
def list_questions(
    is_confirmed: Optional[int] = Query(None, description="0 for pending review, 1 for confirmed"),
    doc_id: Optional[str] = None,
    year: Optional[int] = None,
    knowledge_point_id: Optional[str] = None,
    is_ai_generated: int = 0
):
    with db_session() as conn:
        cursor = conn.cursor()
        
        # Build query
        where_clauses = ["is_ai_generated = ?"]
        params = [is_ai_generated]

        if is_confirmed is not None:
            where_clauses.append("is_confirmed = ?")
            params.append(is_confirmed)
        if doc_id:
            where_clauses.append("doc_id = ?")
            params.append(doc_id)
        if year:
            where_clauses.append("year = ?")
            params.append(year)
        if knowledge_point_id:
            where_clauses.append("id IN (SELECT question_id FROM question_knowledge_rel WHERE knowledge_point_id = ?)")
            params.append(knowledge_point_id)

        sql = f"SELECT * FROM questions WHERE {' AND '.join(where_clauses)} ORDER BY year DESC, question_num ASC"
        cursor.execute(sql, tuple(params))
        q_rows = [dict(r) for r in cursor.fetchall()]

        # Fetch knowledge point relations
        cursor.execute("""
        SELECT rel.question_id, kp.id, kp.name
        FROM question_knowledge_rel rel
        JOIN knowledge_points kp ON rel.knowledge_point_id = kp.id
        """)
        rel_rows = cursor.fetchall()
        kp_map = {}
        for r in rel_rows:
            qid = r["question_id"]
            if qid not in kp_map:
                kp_map[qid] = []
            kp_map[qid].append({"id": r["id"], "name": r["name"]})

        return [_format_question_row(r, kp_map) for r in q_rows]


@router.get("/answer-candidates", response_model=List[AnswerCandidateResponse])
def list_answer_candidates(status: Optional[str] = Query("pending")):
    with db_session() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM answer_candidates WHERE status = ? ORDER BY year DESC, question_num ASC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM answer_candidates ORDER BY year DESC, question_num ASC"
            ).fetchall()
        candidates = []
        for row in rows:
            data = dict(row)
            data["created_at"] = str(data["created_at"])
            candidates.append(AnswerCandidateResponse(**data))
        return candidates

@router.get("/{question_id}", response_model=QuestionResponse)
def get_question(question_id: str):
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM questions WHERE id = ?", (question_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="试题不存在")

        cursor.execute("""
        SELECT kp.id, kp.name
        FROM question_knowledge_rel rel
        JOIN knowledge_points kp ON rel.knowledge_point_id = kp.id
        WHERE rel.question_id = ?
        """, (question_id,))
        kps = [dict(r) for r in cursor.fetchall()]
        kp_map = {question_id: kps}

        return _format_question_row(dict(row), kp_map)

@router.post("", response_model=QuestionResponse)
def create_question(req: QuestionCreate):
    if req.is_confirmed and req.year is None:
        raise HTTPException(status_code=400, detail="确认题目前必须补充年份")
    qid = str(uuid.uuid4())
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("""
        INSERT INTO questions (
            id, doc_id, year, session_name, question_num, question_type, score,
            stem, standard_answer, source_location, source_chunk_id, source_page,
            source_paragraph_ref, answer_source_chunk_id, answer_source_doc_id, answer_source_page,
            answer_source_paragraph_ref, is_confirmed, is_ai_generated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """, (
            qid, req.doc_id, req.year, req.session_name, req.question_num,
            req.question_type, req.score, req.stem, req.standard_answer,
            req.source_location, req.source_chunk_id, req.source_page,
            req.source_paragraph_ref, req.answer_source_chunk_id, req.answer_source_doc_id,
            req.answer_source_page, req.answer_source_paragraph_ref, req.is_confirmed
        ))

        # Insert knowledge point relations
        for kpid in req.knowledge_point_ids:
            cursor.execute("""
            INSERT OR IGNORE INTO question_knowledge_rel (id, question_id, knowledge_point_id)
            VALUES (?, ?, ?)
            """, (str(uuid.uuid4()), qid, kpid))

    return get_question(qid)

@router.put("/{question_id}", response_model=QuestionResponse)
def update_question(question_id: str, req: QuestionUpdate):
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM questions WHERE id = ?", (question_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="试题不存在")

        changed_fields = req.model_fields_set
        effective_year = req.year if "year" in changed_fields else row["year"]
        if req.is_confirmed == 1 and effective_year is None:
            raise HTTPException(status_code=400, detail="确认题目前必须补充年份")
        if req.score is not None and req.score < 0:
            raise HTTPException(status_code=400, detail="分值不能为负数")

        fields = []
        params = []
        if "year" in changed_fields:
            fields.append("year = ?")
            params.append(req.year)
        if "session_name" in changed_fields:
            fields.append("session_name = ?")
            params.append(req.session_name)
        if "question_num" in changed_fields:
            fields.append("question_num = ?")
            params.append(req.question_num)
        if "question_type" in changed_fields:
            fields.append("question_type = ?")
            params.append(req.question_type)
        if "score" in changed_fields:
            fields.append("score = ?")
            params.append(req.score)
        if "stem" in changed_fields:
            fields.append("stem = ?")
            params.append(req.stem)
        if "standard_answer" in changed_fields:
            fields.append("standard_answer = ?")
            params.append(req.standard_answer)
        if "source_location" in changed_fields:
            fields.append("source_location = ?")
            params.append(req.source_location)
        for field_name in (
            "source_chunk_id", "source_page", "source_paragraph_ref",
            "answer_source_chunk_id", "answer_source_doc_id", "answer_source_page", "answer_source_paragraph_ref",
        ):
            value = getattr(req, field_name)
            if field_name in changed_fields:
                fields.append(f"{field_name} = ?")
                params.append(value)
        if req.is_confirmed is not None:
            fields.append("is_confirmed = ?")
            params.append(req.is_confirmed)

        if fields:
            params.append(question_id)
            cursor.execute(f"UPDATE questions SET {', '.join(fields)} WHERE id = ?", tuple(params))

        if req.knowledge_point_ids is not None:
            cursor.execute("DELETE FROM question_knowledge_rel WHERE question_id = ?", (question_id,))
            for kpid in req.knowledge_point_ids:
                cursor.execute("""
                INSERT OR IGNORE INTO question_knowledge_rel (id, question_id, knowledge_point_id)
                VALUES (?, ?, ?)
                """, (str(uuid.uuid4()), question_id, kpid))

            # Mark associated study cards as needing update
            cursor.execute("""
            UPDATE study_cards SET needs_update = 1 WHERE knowledge_point_id IN (
                SELECT knowledge_point_id FROM question_knowledge_rel WHERE question_id = ?
            )
            """, (question_id,))

        content_changed = bool(
            changed_fields.intersection({
                "year", "session_name", "question_num", "question_type", "score",
                "stem", "standard_answer", "source_location", "source_chunk_id",
                "source_page", "source_paragraph_ref", "answer_source_chunk_id",
                "answer_source_doc_id", "answer_source_page", "answer_source_paragraph_ref",
            })
        )
        if content_changed:
            cursor.execute(
                "UPDATE questions SET is_confirmed = 0, needs_review = 1, review_reason = ? WHERE id = ?",
                ("题目或来源已修改，需重新确认", question_id),
            )

    return get_question(question_id)

@router.post("/{question_id}/confirm", response_model=QuestionResponse)
def confirm_question(question_id: str):
    """User confirms question to enter formal statistics pool."""
    return update_question(question_id, QuestionUpdate(is_confirmed=1))

@router.post("/batch-confirm")
def batch_confirm_questions(question_ids: List[str]):
    with db_session() as conn:
        cursor = conn.cursor()
        for qid in question_ids:
            cursor.execute("SELECT year FROM questions WHERE id = ?", (qid,))
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"试题不存在: {qid}")
            if row["year"] is None:
                raise HTTPException(status_code=400, detail=f"试题 {qid} 缺少年份，不能确认")
            cursor.execute("UPDATE questions SET is_confirmed = 1 WHERE id = ?", (qid,))
    return {"status": "ok", "confirmed_count": len(question_ids)}


@router.post("/{question_id}/attach-answer/{candidate_id}", response_model=QuestionResponse)
def attach_answer_candidate(question_id: str, candidate_id: str):
    with db_session() as conn:
        question = conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()
        candidate = conn.execute("SELECT * FROM answer_candidates WHERE id = ?", (candidate_id,)).fetchone()
        if not question:
            raise HTTPException(status_code=404, detail="试题不存在")
        if not candidate:
            raise HTTPException(status_code=404, detail="答案候选不存在")
        if candidate["status"] != "pending":
            raise HTTPException(status_code=400, detail="该答案候选已处理")
        if candidate["year"] is not None and question["year"] is not None and candidate["year"] != question["year"]:
            raise HTTPException(status_code=400, detail="答案年份与题目年份不一致")
        if candidate["session_name"] and question["session_name"] and candidate["session_name"] != question["session_name"]:
            raise HTTPException(status_code=400, detail="答案卷别与题目卷别不一致")

        conn.execute("""
            UPDATE questions
            SET standard_answer = ?, answer_source_chunk_id = ?, answer_source_doc_id = ?, answer_source_page = ?,
                answer_source_paragraph_ref = ?, is_confirmed = 0, needs_review = 1,
                review_reason = ?
            WHERE id = ?
        """, (
            candidate["answer_text"], candidate["source_chunk_id"], candidate["doc_id"], candidate["source_page"],
            candidate["source_paragraph_ref"], "已配对答案，需重新确认", question_id,
        ))
        conn.execute(
            "UPDATE answer_candidates SET status = 'linked', question_id = ? WHERE id = ?",
            (question_id, candidate_id),
        )
        conn.execute("""
            UPDATE study_cards SET needs_update = 1
            WHERE knowledge_point_id IN (
                SELECT knowledge_point_id FROM question_knowledge_rel WHERE question_id = ?
            )
        """, (question_id,))
    return get_question(question_id)

@router.delete("/{question_id}")
def delete_question(question_id: str):
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM questions WHERE id = ?", (question_id,))
    return {"status": "ok", "message": "题目已删除"}
