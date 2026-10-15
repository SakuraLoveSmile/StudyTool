import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.database import db_session
from app.main import app


client = TestClient(app)


def seed_batch(batch_id="ext-review", *, status="completed", candidate_count=1):
    content = "当前教材原文明确说明计划、组织、领导和控制。"
    with db_session() as conn:
        conn.execute(
            "INSERT INTO documents (id, filename, file_hash, file_type, status) VALUES (?, ?, ?, ?, ?)",
            ("doc-review", "管理学.pdf", "hash-review", "textbook", "completed"),
        )
        conn.execute(
            "INSERT INTO document_chunks (id, doc_id, chunk_index, content) VALUES (?, ?, ?, ?)",
            ("chunk-review", "doc-review", 0, content),
        )
        conn.execute(
            "INSERT INTO extraction_batches (id, doc_ids_json, status) VALUES (?, ?, ?)",
            (batch_id, json.dumps(["doc-review"]), status),
        )
        citation = {
            "chunk_id": "chunk-review", "doc_id": "doc-review", "doc_name": "管理学.pdf",
            "quote": "计划、组织、领导和控制", "source_hash": hashlib.sha256(content.encode()).hexdigest(),
        }
        for index in range(candidate_count):
            conn.execute(
                "INSERT INTO extraction_candidates (id, extraction_id, name, citations_json, status) "
                "VALUES (?, ?, ?, ?, 'pending')",
                (f"candidate-{index}", batch_id, f"考点{index}", json.dumps([citation], ensure_ascii=False)),
            )
    return content


def apply(batch_id="ext-review", candidate_id="candidate-0", action="accept", version=1):
    return client.post(
        f"/api/analysis/extractions/{batch_id}/apply",
        json={"decisions": [{"candidate_id": candidate_id, "action": action, "version": version}]},
    )


def test_batch_list_empty_database():
    response = client.get("/api/analysis/extractions?pending_only=true")
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}


def test_batch_list_is_read_only_and_paginates_452_candidates(monkeypatch):
    seed_batch(candidate_count=452)
    from app.services.task_worker import TaskWorker

    monkeypatch.setattr(TaskWorker, "create_task", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected task")))
    listed = client.get("/api/analysis/extractions?limit=1&offset=0")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["pending_candidates"] == 452
    assert client.get("/api/analysis/extractions?limit=1&offset=1").json() == {"items": [], "total": 1}
    assert client.get("/api/analysis/extractions?pending_only=true").json()["total"] == 1
    assert client.get("/api/analysis/extractions?limit=0").status_code == 422
    assert client.get("/api/analysis/extractions?offset=-1").status_code == 422


def test_source_review_returns_current_text_and_rejects_stale_hash():
    seed_batch()
    with db_session() as conn:
        conn.execute("UPDATE extraction_candidates SET source_status = 'invalid', version = 2 WHERE id = 'candidate-0'")
        conn.execute("UPDATE document_chunks SET content = ? WHERE id = 'chunk-review'", ("新原文说明管理控制需要反馈。",))
    endpoint = "/api/analysis/extractions/ext-review/candidates/candidate-0/source-review"
    review = client.get(endpoint)
    assert review.status_code == 200
    body = review.json()
    assert body["can_confirm"] is True
    assert body["citations"][0]["content"] == "新原文说明管理控制需要反馈。"
    assert body["citations"][0]["source_hash"] == hashlib.sha256(body["citations"][0]["content"].encode()).hexdigest()

    payload = {"version": body["version"], "name": body["name"], "chapter": body["chapter"], "citations": body["citations"]}
    with db_session() as conn:
        conn.execute("UPDATE document_chunks SET content = ? WHERE id = 'chunk-review'", ("再次校对后的原文。",))
    assert client.post(endpoint, json=payload).status_code == 409
    current = client.get(endpoint).json()
    payload["citations"] = current["citations"]
    confirmed = client.post(endpoint, json=payload)
    assert confirmed.status_code == 200
    assert confirmed.json()["source_status"] == "valid"
    assert confirmed.json()["version"] == 3
    assert client.post(endpoint, json=payload).status_code == 409


def test_apply_is_idempotent_and_rejects_conflicts_atomically():
    seed_batch(candidate_count=2)
    first = apply()
    assert first.status_code == 200
    assert first.json()["applied_count"] == 1
    replay = apply()
    assert replay.status_code == 200
    assert replay.json()["applied_count"] == 0
    assert replay.json()["replayed_count"] == 1
    assert apply(action="ignore").status_code == 409

    duplicate = client.post(
        "/api/analysis/extractions/ext-review/apply",
        json={"decisions": [{"candidate_id": "candidate-1", "action": "accept", "version": 1}] * 2},
    )
    assert duplicate.status_code == 409
    mixed = client.post(
        "/api/analysis/extractions/ext-review/apply",
        json={"decisions": [
            {"candidate_id": "candidate-1", "action": "accept", "version": 1},
            {"candidate_id": "missing", "action": "accept", "version": 1},
        ]},
    )
    assert mixed.status_code != 200
    with db_session() as conn:
        assert conn.execute("SELECT count(*) FROM knowledge_points WHERE name = '考点0'").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM knowledge_points WHERE name = '考点1'").fetchone()[0] == 0
        assert conn.execute("SELECT status FROM extraction_candidates WHERE id = 'candidate-1'").fetchone()[0] == "pending"


def test_concurrent_apply_creates_one_knowledge_point():
    seed_batch()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: apply(), range(2)))
    assert sorted(result.status_code for result in results) == [200, 200]
    assert sorted(result.json()["applied_count"] for result in results) == [0, 1]
    with db_session() as conn:
        assert conn.execute("SELECT count(*) FROM knowledge_points WHERE name = '考点0'").fetchone()[0] == 1


def test_source_edit_preserves_prior_decisions_and_requires_pending_review(monkeypatch):
    seed_batch(candidate_count=2)
    assert apply(candidate_id="candidate-0").status_code == 200
    with db_session() as conn:
        target = conn.execute("SELECT target_kp_id FROM extraction_candidates WHERE id = 'candidate-0'").fetchone()[0]
        conn.execute(
            "UPDATE extraction_candidates SET matched_kp_id = ?, match_type = 'existing' WHERE id = 'candidate-1'",
            (target,),
        )
    from app.services.task_worker import TaskWorker

    monkeypatch.setattr(TaskWorker, "create_task", lambda *args, **kwargs: "task-mock")
    edited = client.put(
        "/api/materials/chunks/chunk-review",
        json={"content": "校对后的原文说明反馈控制需要定期修正。"},
    )
    assert edited.status_code == 200
    with db_session() as conn:
        accepted = conn.execute("SELECT status, source_status, version FROM extraction_candidates WHERE id = 'candidate-0'").fetchone()
        pending = conn.execute("SELECT status, source_status, version, match_type FROM extraction_candidates WHERE id = 'candidate-1'").fetchone()
        assert tuple(accepted) == ("accepted", "invalid", 3)
        assert tuple(pending) == ("pending", "invalid", 2, "existing")
    assert apply(candidate_id="candidate-1").status_code == 409
    review = client.get("/api/analysis/extractions/ext-review/candidates/candidate-1/source-review")
    assert review.status_code == 200
    assert review.json()["can_confirm"] is True
    payload = review.json()
    confirmed = client.post(
        "/api/analysis/extractions/ext-review/candidates/candidate-1/source-review",
        json={"version": payload["version"], "name": payload["name"], "chapter": payload["chapter"], "citations": payload["citations"]},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["match_type"] == "existing"


def test_legacy_invalid_candidate_stays_read_only():
    seed_batch()
    with db_session() as conn:
        conn.execute("UPDATE extraction_candidates SET status = 'invalid', source_status = 'invalid' WHERE id = 'candidate-0'")
    endpoint = "/api/analysis/extractions/ext-review/candidates/candidate-0/source-review"
    review = client.get(endpoint)
    assert review.status_code == 200
    assert review.json()["can_confirm"] is False
    assert "原审核决定无法确认" in review.json()["reason"]
    assert client.post(endpoint, json={
        "version": 1, "name": "考点0", "chapter": None, "citations": review.json()["citations"],
    }).status_code == 409
    with db_session() as conn:
        assert conn.execute("SELECT count(*) FROM knowledge_points").fetchone()[0] == 0
