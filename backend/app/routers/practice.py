import json
import logging
import uuid
import asyncio
import re
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, HTTPException, Header
from app.database import db_session
from app.config import settings
from app.services.request_operations import run_idempotent
from app.models.schemas import (
    PracticeGenerateRequest, PracticeSubmitRequest, PracticeSessionResponse,
    QuestionEvaluationResult, KeypointCoverage
)
from app.services.retrieval.hybrid import HybridSearchService
from app.services.llm.client import LLMClient
from app.services.llm.prompts import PRACTICE_GENERATE_SYSTEM_PROMPT, PRACTICE_GRADE_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/practice", tags=["practice"])

async def _generate(req: PracticeGenerateRequest):
    """
    Generates 1-5 short answer/essay questions for a knowledge point.
    Pre-generates and encrypts/stores reference answers and rubric in DB.
    """
    kp = None
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, chapter FROM knowledge_points WHERE id = ? AND is_deleted = 0", (req.knowledge_point_id,))
        row = cursor.fetchone()
        if row:
            kp = dict(row)

    if not kp:
        raise HTTPException(status_code=404, detail="知识点不存在或已被删除")

    # Hybrid retrieve relevant material chunks
    query = f"{kp['name']} {kp['chapter'] or ''}"
    chunks = await HybridSearchService.search(query, top_k=4)
    material_context = "\n\n".join([f"[{c['doc_name']}]:\n{c['content']}" for c in chunks])

    user_prompt = f"""知识点名称: {kp['name']}
所属章节: {kp['chapter'] or '未指定'}
目标生成题量: {req.question_count}
考查难度: {'基础题（概念与特征）' if req.difficulty == 'basic' else '综合应用题（对比与场景分析）'}

参考教材资料：
{material_context or '无相关教材切片'}
"""

    res = await LLMClient.chat_json(PRACTICE_GENERATE_SYSTEM_PROMPT, user_prompt)
    raw_questions = res.get("questions", [])

    if not raw_questions:
        if not settings.DEMO_MODE:
            raise HTTPException(status_code=502, detail="模型未返回有效题目")
        raw_questions = [{
            "stem": f"简述【{kp['name']}】的核心概念内涵与主要特征。",
            "reference_answer": "包含核心概念定义、主要特征要素以及应用场景说明。",
            "evaluation_points": [
                {"point": "准确给出核心概念完整定义", "score": 4.0, "basis": "教材第一节"},
                {"point": "列举并阐述至少两条关键特征", "score": 6.0, "basis": "教材特征分析"}
            ],
            "basis": "学科基础概念考查"
        }]

    # Format questions with unique IDs
    stored_questions = []
    public_questions = []
    for q in raw_questions:
        qid = str(uuid.uuid4())
        stored_questions.append({
            "question_id": qid,
            "stem": q.get("stem", ""),
            "reference_answer": q.get("reference_answer", ""),
            "evaluation_points": q.get("evaluation_points", []),
            "basis": q.get("basis", "")
        })
        # Public view hides reference_answer and evaluation points before submission
        public_questions.append({
            "question_id": qid,
            "stem": q.get("stem", "")
        })

    session_id = str(uuid.uuid4())
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("""
        INSERT INTO practice_sessions (
            id, knowledge_point_id, question_count, difficulty, questions_json, created_at
        ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (
            session_id,
            req.knowledge_point_id,
            len(stored_questions),
            req.difficulty,
            json.dumps(stored_questions, ensure_ascii=False)
        ))

    return PracticeSessionResponse(
        id=session_id,
        knowledge_point_id=kp["id"],
        knowledge_point_name=kp["name"],
        question_count=len(stored_questions),
        difficulty=req.difficulty,
        questions=public_questions,
        user_answers=None,
        evaluation=None,
        created_at="刚刚"
    )

@router.get("/sessions", response_model=List[PracticeSessionResponse])
def list_practice_sessions():
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("""
        SELECT s.*, kp.name as kp_name
        FROM practice_sessions s
        LEFT JOIN knowledge_points kp ON s.knowledge_point_id = kp.id
        ORDER BY s.created_at DESC
        LIMIT 30
        """)
        rows = cursor.fetchall()
        result = []
        for r in rows:
            stored_q = json.loads(r["questions_json"])
            user_ans = json.loads(r["user_answers_json"]) if r["user_answers_json"] else None
            eval_data = json.loads(r["evaluation_json"]) if r["evaluation_json"] else None
            
            # If not submitted yet, hide answer details
            public_q = []
            for q in stored_q:
                if user_ans:
                    public_q.append(q)
                else:
                    public_q.append({"question_id": q["question_id"], "stem": q["stem"]})

            result.append(PracticeSessionResponse(
                id=r["id"],
                knowledge_point_id=r["knowledge_point_id"],
                knowledge_point_name=r["kp_name"] or "已归档考点",
                question_count=r["question_count"],
                difficulty=r["difficulty"],
                questions=public_q,
                user_answers=user_ans,
                evaluation=eval_data,
                created_at=str(r["created_at"])
            ))
        return result

@router.get("/sessions/{session_id}", response_model=PracticeSessionResponse)
def get_practice_session(session_id: str):
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("""
        SELECT s.*, kp.name as kp_name
        FROM practice_sessions s
        LEFT JOIN knowledge_points kp ON s.knowledge_point_id = kp.id
        WHERE s.id = ?
        """, (session_id,))
        r = cursor.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="练习记录不存在")

        stored_q = json.loads(r["questions_json"])
        user_ans = json.loads(r["user_answers_json"]) if r["user_answers_json"] else None
        eval_data = json.loads(r["evaluation_json"]) if r["evaluation_json"] else None

        public_q = []
        for q in stored_q:
            if user_ans:
                public_q.append(q)
            else:
                public_q.append({"question_id": q["question_id"], "stem": q["stem"]})

        return PracticeSessionResponse(
            id=r["id"],
            knowledge_point_id=r["knowledge_point_id"],
            knowledge_point_name=r["kp_name"] or "已归档考点",
            question_count=r["question_count"],
            difficulty=r["difficulty"],
            questions=public_q,
            user_answers=user_ans,
            evaluation=eval_data,
            created_at=str(r["created_at"])
        )

async def _submit(session_id: str, req: PracticeSubmitRequest):
    """
    Submits user answers, runs item-by-item semantic evaluation using the saved evaluation points,
    and returns comprehensive feedback.
    """
    session_data = None
    kp_name = ""
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("""
        SELECT s.*, kp.name as kp_name
        FROM practice_sessions s
        LEFT JOIN knowledge_points kp ON s.knowledge_point_id = kp.id
        WHERE s.id = ?
        """, (session_id,))
        row = cursor.fetchone()
        if row:
            session_data = dict(row)
            kp_name = row["kp_name"] or "已归档考点"

    if not session_data:
        raise HTTPException(status_code=404, detail="练习会话不存在")

    stored_questions = json.loads(session_data["questions_json"])
    stored_q_map = {q["question_id"]: q for q in stored_questions}
    user_answers_map = {a.question_id: a.answer_text for a in req.answers}

    def _parse_score(score_raw: Any) -> Optional[float]:
        if score_raw is None:
            return None
        if isinstance(score_raw, (int, float)):
            return float(score_raw)
        if isinstance(score_raw, str):
            match = re.search(r"[-+]?\d*\.?\d+", score_raw)
            if match:
                try:
                    return float(match.group(0))
                except ValueError:
                    return None
        return None

    async def _evaluate_single(q: dict) -> QuestionEvaluationResult:
        qid = q["question_id"]
        u_ans = user_answers_map.get(qid, "").strip()

        eval_points = q.get("evaluation_points", [])
        ref_answer = q.get("reference_answer", "")
        stem = q.get("stem", "")

        user_prompt = f"""【练习题目】: {stem}
【标准参考答案】:
{ref_answer}

【出题时预设的评分要点及分值细则】:
{json.dumps(eval_points, ensure_ascii=False)}

【学生的实际作答内容】:
{u_ans or '【学生未作答（空白）】'}
"""

        res = await LLMClient.chat_json(PRACTICE_GRADE_SYSTEM_PROMPT, user_prompt)

        # Parse keypoints coverage
        raw_cov = res.get("keypoints_coverage", [])
        cov_items = []
        for it in raw_cov:
            cov_items.append(KeypointCoverage(
                point=it.get("point", "评分要点"),
                status=it.get("status", "MISSING"),
                detail=it.get("detail", "未检测到对应论述"),
                basis=it.get("basis", "教材理论")
            ))

        return QuestionEvaluationResult(
            question_id=qid,
            score_estimate=_parse_score(res.get("score_estimate")),
            max_score=_parse_score(res.get("max_score")),
            score_basis=res.get("score_basis"),
            keypoints_coverage=cov_items,
            conceptual_errors=res.get("conceptual_errors", []),
            ungrounded_claims=res.get("ungrounded_claims", []),
            expression_rewrite=res.get("expression_rewrite", "答题时应紧扣概念要点，分条列出。")
        )

    evaluations: List[QuestionEvaluationResult] = list(
        await asyncio.gather(*[_evaluate_single(q) for q in stored_questions])
    )

    # Save to database
    user_answers_list = [{"question_id": a.question_id, "answer_text": a.answer_text} for a in req.answers]
    eval_json_str = json.dumps([e.model_dump() for e in evaluations], ensure_ascii=False)
    user_ans_json_str = json.dumps(user_answers_list, ensure_ascii=False)

    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("""
        UPDATE practice_sessions
        SET user_answers_json = ?, evaluation_json = ?
        WHERE id = ?
        """, (user_ans_json_str, eval_json_str, session_id))

    return PracticeSessionResponse(
        id=session_id,
        knowledge_point_id=session_data["knowledge_point_id"],
        knowledge_point_name=kp_name,
        question_count=len(stored_questions),
        difficulty=session_data["difficulty"],
        questions=stored_questions,
        user_answers=user_answers_list,
        evaluation=evaluations,
        created_at=str(session_data["created_at"])
    )


@router.post("/generate", response_model=PracticeSessionResponse)
async def generate_practice(
    req: PracticeGenerateRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    return await run_idempotent(
        "practice.generate", idempotency_key, req.model_dump(),
        lambda: _generate(req), PracticeSessionResponse,
    )


@router.post("/sessions/{session_id}/submit", response_model=PracticeSessionResponse)
async def submit_and_evaluate_practice(
    session_id: str, req: PracticeSubmitRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    return await run_idempotent(
        "practice.submit:" + session_id, idempotency_key, req.model_dump(),
        lambda: _submit(session_id, req), PracticeSessionResponse,
    )
