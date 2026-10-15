import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.database import init_db

@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        yield c

def test_health_check(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"

def test_config_status(client):
    res = client.get("/api/config/status")
    assert res.status_code == 200
    data = res.json()
    assert "llm_model" in data
    assert "ocr_mode" in data

def test_crud_knowledge_point(client):
    # Create
    create_res = client.post("/api/analysis/knowledge-points", json={
        "code": "4.1",
        "name": "目标管理理论",
        "chapter": "第四章 计划与目标",
        "priority_tag": "important",
        "sort_order": 10
    })
    assert create_res.status_code == 200
    kp_data = create_res.json()
    kp_id = kp_data["id"]
    assert kp_data["name"] == "目标管理理论"

    # Update
    update_res = client.put(f"/api/analysis/knowledge-points/{kp_id}", json={
        "priority_tag": "deferred"
    })
    assert update_res.status_code == 200
    assert update_res.json()["priority_tag"] == "deferred"

    # Delete
    del_res = client.delete(f"/api/analysis/knowledge-points/{kp_id}")
    assert del_res.status_code == 200

def test_practice_generate_and_submit(client):
    # 1. Create a temporary knowledge point
    kp_res = client.post("/api/analysis/knowledge-points", json={
        "code": "5.1",
        "name": "双因素激励理论",
        "chapter": "第五章 激励",
        "priority_tag": "normal",
        "sort_order": 1
    })
    kp_id = kp_res.json()["id"]

    # 2. Generate practice
    gen_res = client.post("/api/practice/generate", json={
        "knowledge_point_id": kp_id,
        "question_count": 1,
        "difficulty": "basic"
    })
    assert gen_res.status_code == 200
    session_data = gen_res.json()
    session_id = session_data["id"]
    assert len(session_data["questions"]) == 1
    qid = session_data["questions"][0]["question_id"]

    # 3. Submit student answer
    submit_res = client.post(f"/api/practice/sessions/{session_id}/submit", json={
        "answers": [
            {"question_id": qid, "answer_text": "双因素理论包括保健因素和激励因素。保健因素只能消除不满，激励因素才能激发积极性。"}
        ]
    })
    assert submit_res.status_code == 200
    eval_data = submit_res.json()
    assert eval_data["evaluation"] is not None
    assert len(eval_data["evaluation"]) == 1
    assert "keypoints_coverage" in eval_data["evaluation"][0]


def test_question_year_validation(client):
    # Year before 1980 is rejected
    res_old = client.post("/api/questions", json={
        "year": 1950,
        "stem": "历史过早题目",
        "question_type": "简答题",
        "is_confirmed": False,
        "knowledge_point_ids": []
    })
    assert res_old.status_code == 422

    # Year far in future is rejected
    res_future = client.post("/api/questions", json={
        "year": 2099,
        "stem": "未来题目",
        "question_type": "简答题",
        "is_confirmed": False,
        "knowledge_point_ids": []
    })
    assert res_future.status_code == 422


@pytest.mark.asyncio
async def test_hybrid_search_graceful_degradation(monkeypatch):
    from app.services.retrieval.hybrid import HybridSearchService
    from app.services.retrieval.embedding import EmbeddingService
    from app.services.retrieval.fts_search import FTSSearchService

    # Mock FTS search to return sample chunks
    monkeypatch.setattr(FTSSearchService, "search", lambda query, limit=12: [
        {"chunk_id": "c1", "content": "管理过程学派", "doc_name": "教材.pdf", "page": 1}
    ])

    # Mock EmbeddingService to raise an error (e.g. network failure or quota)
    async def mock_fail_embedding(text):
        raise ConnectionError("Embedding service unreachable")

    monkeypatch.setattr(EmbeddingService, "get_embedding", mock_fail_embedding)

    results = await HybridSearchService.search("管理学派", top_k=5)
    assert len(results) == 1
    assert results[0]["chunk_id"] == "c1"
    assert results[0].get("degraded") is True
