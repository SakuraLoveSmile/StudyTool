import json
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.database import db_session
from app.services.knowledge_extractor import KnowledgeExtractor
from app.services.task_worker import TaskWorker
from app.models.schemas import CandidateDecisionItem, QuestionLinkDecision

client = TestClient(app)

def _setup_test_doc_and_chunks():
    doc_id = "doc-test-extract-001"
    with db_session() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO documents (id, filename, file_hash, file_type, status)
            VALUES (?, '管理学基础教材.pdf', 'hash-test-001', 'textbook', 'completed')
        """, (doc_id,))
        conn.execute("""

            INSERT OR REPLACE INTO document_chunks (id, doc_id, chunk_index, page_number, paragraph_ref, content)
            VALUES 
            ('chk-ext-1', ?, 0, 1, '第1节', '管理职能是指管理者在管理活动中应当履行的基本职责，包括计划、组织、领导和控制。'),
            ('chk-ext-2', ?, 1, 2, '第2节', '控制过程包含三个关键步骤：确立标准、衡量成效、纠正偏差。控制是闭环管理的核心机制。')
        """, (doc_id, doc_id))
    return doc_id

def test_create_extraction_batch_validation():
    # 1. Empty doc_ids
    res = client.post("/api/analysis/extractions", json={"doc_ids": []})
    assert res.status_code == 400

    # 2. Non-existent doc_id
    res = client.post("/api/analysis/extractions", json={"doc_ids": ["non-existent-doc"]})
    assert res.status_code == 400

    # 3. Valid document with chunks
    doc_id = _setup_test_doc_and_chunks()
    res = client.post("/api/analysis/extractions", json={"doc_ids": [doc_id]})
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "processing"
    assert data["doc_ids"] == [doc_id]
    assert data["task_id"] is not None

    batch_id = data["id"]
    get_res = client.get(f"/api/analysis/extractions/{batch_id}")
    assert get_res.status_code == 200
    assert get_res.json()["id"] == batch_id

def test_extraction_execution_and_citations(monkeypatch):
    doc_id = _setup_test_doc_and_chunks()
    # Create batch
    batch_dict = KnowledgeExtractor.create_extraction_batch([doc_id])
    batch_id = batch_dict["id"]
    task_id = batch_dict["task_id"]

    # Mock LLMClient.chat_json to return candidates with one valid chunk and one fake chunk
    async def mock_chat_json(system_prompt, user_prompt, **kwargs):
        return {
            "candidates": [
                {
                    "name": "管理职能与控制过程",
                    "chapter": "第一章 管理概述",
                    "definition": "管理活动中应当履行的基本职责及闭环控制机制。",
                    "source_chunk_ids": ["chk-ext-1", "fake-hallucinated-chunk-999"],
                    "quote": "管理职能是指管理者在管理活动中应当履行的基本职责"
                }
            ]
        }

    from app.services.llm.client import LLMClient
    monkeypatch.setattr(LLMClient, "chat_json", mock_chat_json)

    # Run execution directly
    import asyncio
    asyncio.run(KnowledgeExtractor.execute_extraction_task(task_id, batch_id))

    # Check candidates
    cands_res = client.get(f"/api/analysis/extractions/{batch_id}/candidates")
    assert cands_res.status_code == 200
    cands = cands_res.json()
    assert len(cands) == 1
    cand = cands[0]
    assert cand["name"] == "管理职能与控制过程"
    assert cand["match_type"] == "new"

    # Verify server-side citation verification: fake chunk must be excluded!
    cits = cand["citations"]
    assert len(cits) == 1
    assert cits[0]["chunk_id"] == "chk-ext-1"
    assert "fake-hallucinated-chunk-999" not in [c["chunk_id"] for c in cits]
    assert cits[0]["doc_id"] == doc_id
    assert cits[0]["doc_name"] == "管理学基础教材.pdf"
    assert cits[0]["page_number"] == 1

def test_candidate_matching_and_apply(monkeypatch):
    doc_id = _setup_test_doc_and_chunks()

    # Pre-insert one soft-deleted knowledge point to test reactivation
    with db_session() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO knowledge_points (id, name, chapter, priority_tag, source_type, is_deleted, deleted_at)
            VALUES ('kp-deleted-001', '控制过程机制', '第4章 控制', 'medium', 'manual', 1, CURRENT_TIMESTAMP)
        """)

    batch_dict = KnowledgeExtractor.create_extraction_batch([doc_id])
    batch_id = batch_dict["id"]
    task_id = batch_dict["task_id"]

    async def mock_chat_json(system_prompt, user_prompt, **kwargs):
        return {
            "candidates": [
                {
                    "name": "控制过程机制", # Matches the deleted KP
                    "chapter": "第4章 控制",
                    "definition": "确立标准与纠偏过程",
                    "source_chunk_ids": ["chk-ext-2"],
                    "quote": "控制过程包含三个关键步骤"
                },
                {
                    "name": "崭新理论考点",
                    "chapter": "第5章 创新",
                    "definition": "全新未见考点",
                    "source_chunk_ids": ["chk-ext-1"],
                    "quote": "管理职能是指管理者"
                }
            ]
        }

    from app.services.llm.client import LLMClient
    monkeypatch.setattr(LLMClient, "chat_json", mock_chat_json)

    import asyncio
    asyncio.run(KnowledgeExtractor.execute_extraction_task(task_id, batch_id))

    cands_res = client.get(f"/api/analysis/extractions/{batch_id}/candidates")
    cands = cands_res.json()
    assert len(cands) == 2

    # Check match types
    matched_cand = next(c for c in cands if c["name"] == "控制过程机制")
    assert matched_cand["match_type"] == "deleted_or_ignored"
    assert matched_cand["matched_kp_id"] == "kp-deleted-001"

    new_cand = next(c for c in cands if c["name"] == "崭新理论考点")
    assert new_cand["match_type"] == "new"

    # Apply decisions
    apply_payload = {
        "decisions": [
            {
                "candidate_id": matched_cand["id"],
                "action": "accept"
            },
            {
                "candidate_id": new_cand["id"],
                "action": "accept",
                "custom_name": "崭新理论考点 (修正名称)"
            }
        ]
    }
    apply_res = client.post(f"/api/analysis/extractions/{batch_id}/apply", json=apply_payload)
    assert apply_res.status_code == 200
    assert apply_res.json()["applied_count"] == 2

    # Check DB state
    with db_session() as conn:
        # kp-deleted-001 should be restored (is_deleted = 0)
        restored = conn.execute("SELECT * FROM knowledge_points WHERE id = 'kp-deleted-001'").fetchone()
        assert restored["is_deleted"] == 0
        assert restored["deleted_at"] is None
        assert restored["source_type"] == "extracted"

        # new KP should exist with custom_name
        new_kp = conn.execute("SELECT * FROM knowledge_points WHERE name = '崭新理论考点 (修正名称)'").fetchone()
        assert new_kp is not None
        assert new_kp["is_deleted"] == 0
        assert new_kp["source_type"] == "extracted"

def test_question_link_suggestions_zero_hallucination():
    doc_id = _setup_test_doc_and_chunks()
    batch_dict = KnowledgeExtractor.create_extraction_batch([doc_id])
    batch_id = batch_dict["id"]

    # 1. When there are ZERO confirmed questions in DB
    with db_session() as conn:
        conn.execute("DELETE FROM questions WHERE is_confirmed = 1")

    res = client.get(f"/api/analysis/extractions/{batch_id}/question-links")
    assert res.status_code == 200
    data = res.json()
    assert data["links"] == []
    assert data["total_confirmed_questions"] == 0
    assert "当前无已确认真题" in data["message"]

    # 2. When confirmed questions DO exist
    with db_session() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO knowledge_points (id, name, chapter, priority_tag, source_type, is_deleted)
            VALUES ('kp-mbo-test', '目标管理理论', '第3章 计划', 'high', 'extracted', 0)
        """)
        conn.execute("""
            INSERT INTO extraction_candidates (id, extraction_id, name, chapter, definition, citations_json, status, match_type, target_kp_id)
            VALUES ('cand-link-1', ?, '目标管理理论', '第3章 计划', '目标展开与自我控制', '[]', 'accepted', 'new', 'kp-mbo-test')
        """, (batch_id,))

        conn.execute("""
            INSERT OR REPLACE INTO questions (id, stem, year, session_name, question_num, is_confirmed)
            VALUES ('q-test-link-1', '请论述德鲁克目标管理理论的主要内容与实施步骤。', 2018, 'A卷', '简答题 2', 1)
        """)

    res2 = client.get(f"/api/analysis/extractions/{batch_id}/question-links")
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["total_confirmed_questions"] >= 1
    assert len(data2["links"]) == 1
    link = data2["links"][0]
    assert link["question_id"] == "q-test-link-1"
    assert link["knowledge_point_id"] == "kp-mbo-test"
    assert link["confidence"] == 1.0

    # 3. Apply the link
    apply_res = client.post(f"/api/analysis/extractions/{batch_id}/question-links/apply", json={
        "links": [
            {
                "question_id": "q-test-link-1",
                "knowledge_point_id": "kp-mbo-test",
                "action": "accept"
            }
        ]
    })
    assert apply_res.status_code == 200
    assert apply_res.json()["applied_count"] == 1

    with db_session() as conn:
        rel = conn.execute("""
            SELECT * FROM question_knowledge_rel WHERE question_id = 'q-test-link-1' AND knowledge_point_id = 'kp-mbo-test'
        """).fetchone()
        assert rel is not None
