import json
import logging
import uuid
from typing import Optional, List
from fastapi import APIRouter, HTTPException, Header
from app.database import db_session
from app.models.schemas import (
    StudyCardResponse, GenerateCardRequest, CitationItem,
    QuestionAnswerRequest, QuestionAnswerResponse, TaskResponse
)
from app.services.task_worker import TaskWorker
from app.services.retrieval.hybrid import HybridSearchService
from app.services.llm.client import LLMClient
from app.services.llm.prompts import ANSWER_ASSISTANT_SYSTEM_PROMPT
from app.services.request_operations import run_idempotent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cards", tags=["cards"])

@router.get("/{knowledge_point_id}", response_model=StudyCardResponse)
def get_study_card(knowledge_point_id: str):
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name FROM knowledge_points WHERE id = ? AND is_deleted = 0", (knowledge_point_id,))
        kp = cursor.fetchone()
        if not kp:
            raise HTTPException(status_code=404, detail="知识点不存在或已被删除")

        cursor.execute("SELECT * FROM study_cards WHERE knowledge_point_id = ?", (knowledge_point_id,))
        card = cursor.fetchone()
        if not card:
            raise HTTPException(status_code=404, detail="尚未为该考点生成学习卡，请点击'生成学习卡'")

        citations_raw = card["citations_json"]
        citations = []
        if citations_raw:
            try:
                items = json.loads(citations_raw)
                citations = [CitationItem(**it) for it in items]
            except Exception:
                pass

        return StudyCardResponse(
            id=card["id"],
            knowledge_point_id=knowledge_point_id,
            knowledge_point_name=kp["name"],
            concise_content=card["concise_content"] or "",
            detailed_content=card["detailed_content"] or "",
            answer_structure=card["answer_structure"] or "",
            confusing_points_and_cases=card["confusing_points_and_cases"] or "",
            citations=citations,
            needs_update=card["needs_update"] or 0,
            updated_at=str(card["updated_at"])
        )

@router.post("/generate", response_model=TaskResponse)
def generate_study_card(req: GenerateCardRequest):
    """Schedules background task to generate study card."""
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name FROM knowledge_points WHERE id = ? AND is_deleted = 0", (req.knowledge_point_id,))
        kp = cursor.fetchone()
        if not kp:
            raise HTTPException(status_code=404, detail="知识点不存在或已被删除")

    task_id = TaskWorker.create_task("generate_card", req.knowledge_point_id, f"生成学习卡: {kp['name']}")
    return TaskWorker.create_task_response(task_id) if hasattr(TaskWorker, "create_task_response") else TaskResponse(
        id=task_id,
        task_type="generate_card",
        target_id=req.knowledge_point_id,
        status="pending",
        progress=0,
        message="已加入生成队列",
        error_msg=None,
        created_at="",
        updated_at=""
    )

async def _question_answer_assistant(req: QuestionAnswerRequest):
    """
    Generates AI reference answer, thought process, citations and conflict notes.
    """
    stem = req.custom_stem or ""
    official_answer = None

    if req.question_id:
        with db_session() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT stem, standard_answer FROM questions WHERE id = ?", (req.question_id,))
            q_row = cursor.fetchone()
            if not q_row:
                raise HTTPException(status_code=404, detail="题目不存在")
            stem = q_row["stem"]
            official_answer = q_row["standard_answer"]

    if not stem:
        raise HTTPException(status_code=400, detail="必须提供题目 ID 或自定义题目内容")

    # Hybrid retrieve relevant textbook context
    retrieved_chunks = await HybridSearchService.search(stem, top_k=4)
    chunks_context = ""
    citations = []
    for c in retrieved_chunks:
        loc = f"第 {c['page_number']} 页" if c['page_number'] else (c['paragraph_ref'] or "段落")
        chunks_context += f"\n--- 资料来源: {c['doc_name']} ({loc}) ---\n{c['content']}\n"
        citations.append(CitationItem(
            chunk_id=c["chunk_id"],
            doc_name=c["doc_name"],
            page_or_ref=loc,
            snippet=c["content"][:120] + "..."
        ))

    user_prompt = f"""问题题干：
{stem}

【已上传的真题/官方参考答案】:
{official_answer or '未提供官方答案'}

【检索到的教材切片依据】:
{chunks_context or '未检索到相关切片'}
"""

    res = await LLMClient.chat_json(ANSWER_ASSISTANT_SYSTEM_PROMPT, user_prompt)

    return QuestionAnswerResponse(
        stem=stem,
        official_answer=official_answer,
        ai_answer=res.get("ai_answer", "解答生成完毕。"),
        thought_process=res.get("thought_process", "本题考查核心概念与特征。"),
        citations=citations,
        conflict_notes=res.get("conflict_notes")
    )

@router.post("/assistant", response_model=QuestionAnswerResponse)
async def question_answer_assistant(req: QuestionAnswerRequest, idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")):
    return await run_idempotent("cards.assistant", idempotency_key, req.model_dump(), lambda: _question_answer_assistant(req), QuestionAnswerResponse)
