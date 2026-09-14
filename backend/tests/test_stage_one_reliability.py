import uuid
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest
from fastapi import HTTPException

from app.config import settings
from app.database import db_session, init_db
from app.routers.questions import confirm_question
from app.routers.questions import create_question, update_question
from app.models.schemas import QuestionCreate, QuestionUpdate
from app.services.llm.client import LLMClient
from app.services.parser.image_parser import ImageParser
from app.services.retrieval.embedding import EmbeddingService
from app.services.task_worker import TaskWorker


@pytest.fixture(autouse=True)
def clean_stage_one_tables():
    init_db()
    with db_session() as conn:
        conn.execute("DELETE FROM answer_candidates")
        conn.execute("DELETE FROM question_knowledge_rel")
        conn.execute("DELETE FROM questions")
        conn.execute("DELETE FROM document_chunks_fts")
        conn.execute("DELETE FROM document_chunks")
        conn.execute("DELETE FROM tasks")
        conn.execute("DELETE FROM documents")


def _insert_exam_document(chunk_count: int = 1) -> tuple[str, list[str]]:
    doc_id = str(uuid.uuid4())
    chunk_ids = []
    with db_session() as conn:
        conn.execute(
            "INSERT INTO documents (id, filename, file_hash, file_type, status) VALUES (?, ?, ?, ?, ?)",
            (doc_id, "exam.pdf", uuid.uuid4().hex, "exam", "completed"),
        )
        for index in range(chunk_count):
            chunk_id = str(uuid.uuid4())
            chunk_ids.append(chunk_id)
            content = f"第 {index + 1} 页：简述组织形式。"
            conn.execute(
                """
                INSERT INTO document_chunks (
                    id, doc_id, chunk_index, page_number, heading, content,
                    content_segmented, original_content, embedding_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'completed')
                """,
                (chunk_id, doc_id, index, index + 1, f"第 {index + 1} 页", content, content, content),
            )
            conn.execute(
                "INSERT INTO document_chunks_fts (chunk_id, content_segmented) VALUES (?, ?)",
                (chunk_id, content),
            )
    return doc_id, chunk_ids


@pytest.mark.asyncio
async def test_unknown_year_is_kept_pending_and_repeated_extraction_is_idempotent():
    doc_id, chunk_ids = _insert_exam_document()
    response = {"questions": [{"year": None, "question_num": "1", "stem": "简述组织形式。"}]}

    for _ in range(2):
        task_id = TaskWorker.create_task("extract_questions", doc_id)
        with patch.object(LLMClient, "chat_json", AsyncMock(return_value=response)):
            await TaskWorker._execute_extract_questions(task_id, doc_id)

    with db_session() as conn:
        question = conn.execute("SELECT * FROM questions").fetchone()
        count = conn.execute("SELECT COUNT(*) AS count FROM questions").fetchone()["count"]
    assert count == 1
    assert question["year"] is None
    assert question["source_chunk_id"] == chunk_ids[0]
    assert question["needs_review"] == 1

    with pytest.raises(HTTPException) as error:
        confirm_question(question["id"])
    assert error.value.status_code == 400


@pytest.mark.asyncio
async def test_failed_extraction_batch_is_reported_as_partial():
    doc_id, _ = _insert_exam_document(chunk_count=9)
    task_id = TaskWorker.create_task("extract_questions", doc_id)
    responses = [
        {"questions": [{"year": 2023, "question_num": "1", "stem": "简述组织形式。"}]},
        RuntimeError("provider timeout"),
    ]
    with patch.object(LLMClient, "chat_json", AsyncMock(side_effect=responses)):
        await TaskWorker._execute_extract_questions(task_id, doc_id)

    with db_session() as conn:
        task = conn.execute("SELECT status, error_msg, batch_errors_json FROM tasks WHERE id = ?", (task_id,)).fetchone()
        count = conn.execute("SELECT COUNT(*) AS count FROM questions").fetchone()["count"]
    assert task["status"] == "partial"
    assert "provider timeout" in task["error_msg"]
    assert "provider timeout" in task["batch_errors_json"]
    assert count == 1


@pytest.mark.asyncio
async def test_invalid_model_score_is_rejected_before_persistence():
    doc_id, _ = _insert_exam_document()
    task_id = TaskWorker.create_task("extract_questions", doc_id)
    with patch.object(LLMClient, "chat_json", AsyncMock(return_value={
        "questions": [{"year": 2023, "question_num": "1", "stem": "题目", "score": "未知"}]
    })):
        await TaskWorker._execute_extract_questions(task_id, doc_id)

    with db_session() as conn:
        task = conn.execute("SELECT status, error_msg FROM tasks WHERE id = ?", (task_id,)).fetchone()
        count = conn.execute("SELECT COUNT(*) AS count FROM questions").fetchone()["count"]
    assert task["status"] == "failed"
    assert "分值" in task["error_msg"]
    assert count == 0


@pytest.mark.asyncio
async def test_ocr_failure_is_not_saved_as_source_text(tmp_path):
    image_path = tmp_path / "scan.png"
    image_path.write_bytes(b"not a real image")
    original_mode = settings.OCR_MODE
    original_key = settings.OCR_API_KEY
    try:
        settings.OCR_MODE = "vision_llm"
        settings.OCR_API_KEY = ""
        chunks = await ImageParser.parse_image(image_path)
    finally:
        settings.OCR_MODE = original_mode
        settings.OCR_API_KEY = original_key

    assert chunks[0]["content"] == ""
    assert chunks[0]["parse_error"]


@pytest.mark.asyncio
async def test_formal_mode_does_not_use_offline_llm_fallback(monkeypatch):
    original_key = settings.LLM_API_KEY
    original_demo = settings.DEMO_MODE
    try:
        settings.LLM_API_KEY = ""
        settings.DEMO_MODE = False
        monkeypatch.delenv("DEMO_MODE", raising=False)
        with pytest.raises(RuntimeError, match="正式模式"):
            await LLMClient.chat_json("提取题目", "原文")
    finally:
        settings.LLM_API_KEY = original_key
        settings.DEMO_MODE = original_demo


def test_create_question_and_explicitly_clear_optional_fields():
    question = create_question(QuestionCreate(
        year=2023,
        score=10,
        stem="测试题",
        source_location="第 1 页",
    ))
    updated = update_question(
        question.id,
        QuestionUpdate(year=None, score=None, source_location=None),
    )
    assert updated.year is None
    assert updated.score is None
    assert updated.source_location is None


@pytest.mark.asyncio
async def test_reindex_does_not_write_vector_for_changed_content():
    doc_id, chunk_ids = _insert_exam_document()
    chunk_id = chunk_ids[0]
    with db_session() as conn:
        conn.execute("UPDATE document_chunks SET embedding_status = 'pending' WHERE id = ?", (chunk_id,))

    async def embedding_that_edits_source(_text):
        with db_session() as conn:
            conn.execute(
                "UPDATE document_chunks SET content = ?, embedding_status = 'pending' WHERE id = ?",
                ("用户再次校对后的内容", chunk_id),
            )
        return np.ones(EmbeddingService.DIMENSION, dtype=np.float32)

    task_id = TaskWorker.create_task("reindex_chunk", chunk_id)
    with patch.object(EmbeddingService, "get_embedding", side_effect=embedding_that_edits_source):
        await TaskWorker._execute_reindex_chunk(task_id, chunk_id)

    with db_session() as conn:
        row = conn.execute(
            "SELECT content, embedding, embedding_status FROM document_chunks WHERE id = ?",
            (chunk_id,),
        ).fetchone()
    assert row["content"] == "用户再次校对后的内容"
    assert row["embedding"] is None
    assert row["embedding_status"] == "pending"
