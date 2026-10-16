"""Offline regression for sources derived from the recall-card picker payload."""

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from app.database import db_session
from app.main import app
from app.services.recall_generator import RecallGenerationError, RecallGenerator


def _seed(*, chunk_count=12, reference="教材.pdf 第 1 页", source_doc_id="doc-1"):
    with db_session() as conn:
        conn.execute(
            "INSERT INTO documents (id, filename, file_hash, file_type, status) "
            "VALUES ('doc-1', '教材.pdf', 'document-file-hash', 'textbook', 'completed')"
        )
        conn.execute(
            "INSERT INTO recall_chapters (id, source_doc_id, title, normalized_title, title_normalized) "
            "VALUES ('ch-1', ?, '第一章 管理', '第一章 管理', '第一章 管理')", (source_doc_id,)
        )
        conn.execute(
            "INSERT INTO knowledge_points (id, name, chapter, source_reference) "
            "VALUES ('kp-1', '管理的定义', '第一章 管理', ?)", (reference,)
        )
        for index in range(chunk_count):
            conn.execute(
                "INSERT INTO document_chunks (id, doc_id, chunk_index, content) VALUES (?, 'doc-1', ?, ?)",
                (f"chunk-{index}", index, f"教材片段{index}：管理是协调资源实现目标的过程。"),
            )


def _citation(chunk_id="chunk-8"):
    with db_session() as conn:
        chunk = conn.execute("SELECT * FROM document_chunks WHERE id = ?", (chunk_id,)).fetchone()
    return {
        "chunk_id": chunk_id, "doc_id": chunk["doc_id"], "quote": "管理是协调资源实现目标的过程",
        "source_hash": hashlib.sha256(chunk["content"].encode()).hexdigest(),
    }


def _candidate(citations, *, status="accepted", source_status="valid", target="kp-1", suffix="1"):
    with db_session() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO extraction_batches (id, doc_ids_json, status) "
            "VALUES ('ex-1', '[\"doc-1\"]', 'completed')"
        )
        conn.execute(
            "INSERT INTO extraction_candidates "
            "(id, extraction_id, name, citations_json, status, source_status, target_kp_id) "
            "VALUES (?, 'ex-1', '管理定义', ?, ?, ?, ?)",
            (f"cand-{suffix}", json.dumps(citations), status, source_status, target),
        )


def _create():
    return RecallGenerator.create_generation_batch("ch-1", knowledge_point_ids=["kp-1"])


def _snapshot(batch):
    with db_session() as conn:
        raw = conn.execute(
            "SELECT input_snapshot_json FROM recall_generation_batches WHERE id = ?", (batch["batch_id"],)
        ).fetchone()[0]
    return json.loads(raw)["units"][0]["chunks"]


@pytest.mark.parametrize("status", ["accepted", "merged"])
def test_accepted_and_merged_sources_choose_cited_chunk_from_large_book(status):
    _seed()
    citation = _citation()
    _candidate([citation, citation], status=status)
    chunks = _snapshot(_create())
    assert [chunk["id"] for chunk in chunks] == ["chunk-8"]
    assert chunks[0]["source_hash"] == "document-file-hash"
    assert chunks[0]["content_hash"] == citation["source_hash"]


def test_manual_structured_reference_reads_sqlite_row_and_deduplicates():
    _seed(reference=json.dumps({"citations": [{"chunk_id": "chunk-7"}], "chunk_ids": ["chunk-7"]}))
    assert [chunk["id"] for chunk in _snapshot(_create())] == ["chunk-7"]


def test_display_reference_is_not_interpreted_as_chunk_id():
    _seed(chunk_count=1, reference="chunk-0")
    with pytest.raises(RecallGenerationError, match="仅有展示说明"):
        _create()


@pytest.mark.parametrize("change,reason", [
    ({"source_hash": "old-hash"}, "原文已变化"),
    ({"quote": "并非当前原文"}, "引用摘录"),
    ({"quote": ""}, "引用摘录"),
    ({"doc_id": "another-doc"}, "资料归属"),
    ({"doc_id": None}, "资料归属"),
    ({"chunk_id": "missing"}, "切片已缺失"),
    ({"source_hash": None}, "原文已变化"),
])
def test_invalid_accepted_citation_is_rejected_without_fallback(change, reason):
    _seed(chunk_count=1, reference=None)
    citation = _citation("chunk-0")
    citation.update(change)
    _candidate([citation])
    with pytest.raises(RecallGenerationError, match=reason):
        _create()
    with db_session() as conn:
        assert conn.execute("SELECT count(*) FROM recall_generation_batches").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


@pytest.mark.parametrize("status", ["invalid", "supplement_needed"])
def test_invalid_source_status_blocks_generation(status):
    _seed()
    _candidate([_citation()], source_status=status)
    with pytest.raises(RecallGenerationError, match="来源已失效"):
        _create()


def test_invalid_knowledge_point_source_status_blocks_generation():
    _seed()
    _candidate([_citation()])
    with db_session() as conn:
        conn.execute("UPDATE knowledge_points SET source_status = 'invalid'")
    with pytest.raises(RecallGenerationError, match="来源已失效"):
        _create()


@pytest.mark.parametrize("citations", [[], {}, [None], [{"quote": "missing chunk"}]])
def test_malformed_or_empty_accepted_sources_do_not_fallback(citations):
    _seed(chunk_count=1, reference=None)
    _candidate(citations)
    with pytest.raises(RecallGenerationError, match="引用|切片编号"):
        _create()


def test_all_candidate_citations_are_checked_before_deduplication():
    _seed()
    citation = _citation()
    _candidate([citation])
    _candidate([{**citation, "source_hash": "changed"}], status="merged", suffix="2")
    with pytest.raises(RecallGenerationError, match="原文已变化"):
        _create()


def test_pending_sources_do_not_supply_current_knowledge_point():
    _seed()
    _candidate([_citation()], status="pending")
    with pytest.raises(RecallGenerationError, match="仅有展示说明"):
        _create()


def test_other_target_sources_do_not_supply_current_knowledge_point():
    _seed()
    with db_session() as conn:
        conn.execute("INSERT INTO knowledge_points (id, name) VALUES ('kp-2', '另一个知识点')")
    _candidate([_citation()], target="kp-2")
    with pytest.raises(RecallGenerationError, match="仅有展示说明"):
        _create()


def test_missing_document_is_rejected_without_partial_snapshot():
    _seed()
    _candidate([_citation()])
    with db_session() as conn:
        conn.execute("DELETE FROM documents WHERE id = 'doc-1'")
    with pytest.raises(RecallGenerationError, match="切片已缺失"):
        _create()


def test_invalid_json_in_reviewed_candidate_is_actionable():
    _seed()
    _candidate([_citation()])
    with db_session() as conn:
        conn.execute("UPDATE extraction_candidates SET citations_json = 'not-json'")
    with pytest.raises(RecallGenerationError, match="引用格式错误"):
        _create()


def test_cross_document_source_is_rejected_even_with_correct_hash_and_quote():
    _seed()
    with db_session() as conn:
        conn.execute("INSERT INTO documents (id, filename, file_hash, file_type) VALUES ('doc-2', '另一本书', 'hash-2', 'textbook')")
        conn.execute("UPDATE document_chunks SET doc_id = 'doc-2' WHERE id = 'chunk-8'")
    _candidate([_citation()])
    with pytest.raises(RecallGenerationError, match="不属于所选章节资料"):
        _create()


def test_cross_chapter_knowledge_point_is_rejected():
    _seed()
    _candidate([_citation()])
    with db_session() as conn:
        conn.execute("UPDATE knowledge_points SET chapter = '第二章 决策'")
    with pytest.raises(RecallGenerationError, match="不属于所选章节"):
        _create()


@pytest.mark.parametrize("count,chars,reason", [(11, 20, "切片数量"), (1, 12001, "输入正文")])
def test_derived_sources_preserve_chunk_and_character_limits(count, chars, reason):
    _seed(chunk_count=count, reference=None)
    with db_session() as conn:
        conn.execute("UPDATE document_chunks SET content = ?", ("管" * chars,))
    _candidate([{**_citation(f"chunk-{i}"), "quote": "管"} for i in range(count)])
    with pytest.raises(RecallGenerationError, match=reason):
        _create()


def test_no_reference_legacy_fallback_preserves_small_chapter_and_large_chapter_limit():
    _seed(chunk_count=2, reference=None)
    assert len(_snapshot(_create())) == 2
    with db_session() as conn:
        for index in range(2, 11):
            conn.execute(
                "INSERT INTO document_chunks (id, doc_id, chunk_index, content) VALUES (?, 'doc-1', ?, '原文')",
                (f"chunk-{index}", index),
            )
    with pytest.raises(RecallGenerationError, match="严格 10 个切片限制"):
        _create()


def test_http_picker_payload_enqueues_and_legacy_missing_ids_returns_chinese_error():
    _seed()
    _candidate([_citation()])
    client = TestClient(app)
    missing = client.post("/api/learning/generations", json={"chapter_id": "ch-1", "instructions": "重点定义"})
    assert missing.status_code == 422
    assert missing.json()["detail"]["message"] == "必须提供知识点单元"
    response = client.post("/api/learning/generations", json={
        "chapter_id": "ch-1", "knowledge_point_ids": ["kp-1"], "instructions": "重点定义",
    })
    assert response.status_code == 200, response.text
    with db_session() as conn:
        assert conn.execute("SELECT count(*) FROM tasks WHERE task_type = 'generate_recall_cards'").fetchone()[0] == 1
    assert [chunk["id"] for chunk in _snapshot({"batch_id": response.json()["id"]})] == ["chunk-8"]
