"""Offline source selection, precise diagnostics, and partial retry regression."""

import copy
import hashlib
import json

import pytest

from app.config import settings
from app.database import db_session
from app.services.llm.client import LLMClient
from app.services.recall_generator import (
    RecallGenerator, RecallValidationError, SourceChangedError,
)
from app.services.task_worker import TaskWorker


def _unit(*, content=" \n会计是现代经济管理的重要组成部分。\r\n", chunk_id="chunk-0", kp_id="kp-0"):
    return RecallGenerator._unit_from_snapshot({
        "knowledge_point_id": kp_id, "knowledge_point_name": "会计概念",
        "chapter_id": "ch-1", "chapter_title": "第一章 总论", "source_doc_id": "doc-1",
        "chunks": [{"id": chunk_id, "doc_id": "doc-1", "doc_name": "会计教材",
                    "source_hash": "file-hash", "content": content, "page_number": 3}],
    })


def _payload(unit):
    prompt = RecallGenerator._build_user_prompt(unit, request_id="request-1", fingerprint="fp", mode="formal")
    return json.loads(prompt.split("模型输入 JSON：\n", 1)[1])


def _response(citations, kp_id="kp-0"):
    return {"knowledge_point_id": kp_id, "cards": [{
        "title": "会计概念", "front_content": "会计是什么？",
        "back_content": "会计是现代经济管理的重要组成部分。", "citations": citations,
    }], "insufficient_reason": None}


def _selected(unit):
    return {"citation_id": _payload(unit)["source_chunks"][0]["passages"][0]["citation_id"]}


def _seed(count=1):
    with db_session() as conn:
        conn.execute("INSERT INTO documents (id,filename,file_hash,file_type,status) "
                     "VALUES ('doc-1','会计教材','file-hash','textbook','completed')")
        conn.execute("INSERT INTO recall_chapters (id,source_doc_id,title,normalized_title,title_normalized) "
                     "VALUES ('ch-1','doc-1','第一章 总论','第一章 总论','第一章 总论')")
        units = []
        for index in range(count):
            unit = _unit(kp_id=f"kp-{index}", chunk_id=f"chunk-{index}",
                         content=f" \n会计知识点{index}：会计是现代经济管理的重要组成部分。\t")
            chunk = unit.chunks[0]
            conn.execute("INSERT INTO document_chunks (id,doc_id,chunk_index,content) VALUES (?,'doc-1',?,?)",
                         (chunk.id, index, chunk.content))
            conn.execute("INSERT INTO knowledge_points (id,name,chapter,source_reference) VALUES (?,?,?,?)",
                         (unit.knowledge_point_id, f"会计知识点{index}", "第一章 总论",
                          json.dumps({"chunk_ids": [chunk.id]})))
            units.append(unit.as_dict())
    return RecallGenerator.create_generation_batch("ch-1", units, request_id="citation-test")


def _events(task_id, event_type=None):
    with db_session() as conn:
        rows = conn.execute("SELECT event_type,stage,unit_index,message,data_json FROM task_events "
                            "WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
    return [dict(row) for row in rows if event_type is None or row["event_type"] == event_type]


def test_passages_send_each_original_character_once_and_restore_whitespace():
    original = "\r\n\t" + ("会计的监督职能与核算职能相辅相成。\n" * 550) + "\u3000\u00a0"
    unit = _unit(content=original)
    source = _payload(unit)["source_chunks"][0]
    assert set(source) == {"chunk_id", "doc_id", "doc_name", "location", "source_hash", "content_hash", "passages"}
    assert "content" not in source
    assert "".join(p["content"] for p in source["passages"]) == original
    assert all(0 < len(p["content"]) <= 600 for p in source["passages"])
    assert len({p["citation_id"] for p in source["passages"]}) == len(source["passages"])
    for passage in source["passages"]:
        cards, reason = RecallGenerator.validate_model_output(_response([{"citation_id": passage["citation_id"]}]), unit)
        citation = cards[0]["citations"][0]
        assert citation == {
            "chunk_id": "chunk-0", "doc_id": "doc-1", "doc_name": "会计教材",
            "source_hash": "file-hash", "content_hash": hashlib.sha256(original.encode()).hexdigest(),
            "page_number": 3, "paragraph_ref": None, "quote": passage["content"],
        }
        assert reason is None


@pytest.mark.parametrize("citation,code", [
    ({}, "citation_id_missing"),
    ({"citation_id": None}, "citation_id_invalid"),
    ({"citation_id": 12}, "citation_id_invalid"),
    ({"citation_id": []}, "citation_id_invalid"),
    ({"citation_id": ""}, "citation_id_invalid"),
    ({"citation_id": "unknown-model-text-must-not-be-logged"}, "citation_id_unknown"),
])
def test_invalid_ids_have_safe_structured_errors(citation, code):
    with pytest.raises(RecallValidationError) as caught:
        RecallGenerator.validate_model_output(_response([citation]), _unit())
    assert caught.value.reason_code == code
    assert caught.value.field == "cards[0].citations[0].citation_id"
    assert "unknown-model-text" not in str(caught.value)


@pytest.mark.parametrize("other", [
    {"content": "全新的教材原文"}, {"chunk_id": "outside-scope"},
])
def test_ids_from_old_text_or_other_scope_are_rejected(other):
    with pytest.raises(RecallValidationError) as caught:
        RecallGenerator.validate_model_output(_response([_selected(_unit(**other))]), _unit())
    assert caught.value.reason_code == "citation_id_unknown"


@pytest.mark.parametrize("key", [
    "chunk_id", "chunkId", "source_chunk_id", "quote", "snippet", "excerpt",
    "source_hash", "document_hash", "file_hash", "hash", "content_hash", "doc_id", "doc_name",
])
def test_mixed_legacy_fields_cannot_override_selected_source(key):
    citation = {**_selected(_unit()), key: "conflicting-model-value"}
    with pytest.raises(RecallValidationError) as caught:
        RecallGenerator.validate_model_output(_response([citation]), _unit())
    assert caught.value.reason_code == "citation_reference_conflict"
    assert caught.value.field == f"cards[0].citations[0].{key}"
    assert "conflicting-model-value" not in str(caught.value)


def test_consistent_extra_fields_are_accepted_but_server_restores_file_hash():
    unit = _unit()
    chunk = unit.chunks[0]
    citation = {**_selected(unit), "chunk_id": chunk.id, "quote": chunk.content,
                "source_hash": chunk.content_hash, "content_hash": chunk.content_hash}
    cards, _ = RecallGenerator.validate_model_output(_response([citation]), unit)
    assert cards[0]["citations"][0]["source_hash"] == "file-hash"
    assert cards[0]["citations"][0]["quote"] == chunk.content


def test_whitespace_only_passage_cannot_support_a_card():
    unit = _unit(content=" " * 650 + "有效正文")
    passages = _payload(unit)["source_chunks"][0]["passages"]
    assert "".join(p["content"] for p in passages) == unit.chunks[0].content
    assert not passages[0]["content"].strip()
    with pytest.raises(RecallValidationError) as caught:
        RecallGenerator.validate_model_output(_response([{"citation_id": passages[0]["citation_id"]}]), unit)
    assert caught.value.reason_code == "citation_quote_missing"
    cards, _ = RecallGenerator.validate_model_output(_response([{"citation_id": passages[-1]["citation_id"]}]), unit)
    assert cards[0]["citations"][0]["quote"] == passages[-1]["content"]


@pytest.mark.parametrize("field,value,code", [
    ("quote", "编造或改写的原文", "citation_quote_not_original"),
    ("chunk_id", "other-scope", "citation_chunk_out_of_scope"),
    ("source_hash", "old-hash", "citation_hash_mismatch"),
])
def test_legacy_response_still_validates_source_without_repair(field, value, code):
    unit = _unit()
    citation = {"chunk_id": "chunk-0", "quote": unit.chunks[0].content.strip(), "source_hash": "file-hash"}
    valid, _ = RecallGenerator.validate_model_output(_response([citation]), unit)
    assert valid[0]["citations"][0]["quote"] == citation["quote"]
    citation[field] = value
    with pytest.raises(RecallValidationError) as caught:
        RecallGenerator.validate_model_output(_response([citation]), unit)
    assert caught.value.reason_code == code


def test_duplicate_selected_citations_and_excess_count_are_rejected():
    citation = _selected(_unit())
    with pytest.raises(RecallValidationError, match="citation_duplicate"):
        RecallGenerator.validate_model_output(_response([citation, citation]), _unit())
    with pytest.raises(RecallValidationError, match="citation_count_invalid"):
        RecallGenerator.validate_model_output(_response([citation] * 11), _unit())


@pytest.mark.asyncio
async def test_partial_six_quote_failures_are_logged_and_retry_only_generates_failed_points(monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    batch = _seed(19)
    calls = []

    async def first_model(system, prompt, **kwargs):
        assert '"citation_id"' in system
        payload = json.loads(prompt.split("模型输入 JSON：\n", 1)[1])
        kp_id = payload["knowledge_point_id"]
        calls.append(kp_id)
        if int(kp_id.split("-")[-1]) < 6:
            return _response([{"chunk_id": payload["source_chunks"][0]["chunk_id"],
                               "quote": "模型改写后的句子", "source_hash": "file-hash"}], kp_id)
        return _response([{"citation_id": payload["source_chunks"][0]["passages"][0]["citation_id"]}], kp_id)

    monkeypatch.setattr(LLMClient, "chat_json", first_model)
    result = await RecallGenerator.execute_generation_task(batch["task_id"], batch["batch_id"])
    assert result["status"] == "partial"
    assert (result["succeeded_units"], result["failed_units"], result["card_count"]) == (13, 6, 13)
    failures = _events(batch["task_id"], "validation_failed")
    assert len(failures) == 6
    for index, event in enumerate(failures):
        data = json.loads(event["data_json"])
        assert event["stage"] == "output_validation"
        assert event["unit_index"] == index + 1
        assert data == {
            "knowledge_point_id": f"kp-{index}", "knowledge_point_name": f"会计知识点{index}",
            "failed_stage": "output_validation", "exception_type": "RecallValidationError",
            "reason_code": "citation_quote_not_original", "field": "cards[0].citations[0].quote",
        }
        assert "模型改写后的句子" not in event["message"]
    with db_session() as conn:
        assert conn.execute("SELECT count(*) FROM recall_cards").fetchone()[0] == 13
        errors = json.loads(conn.execute("SELECT batch_errors_json FROM tasks WHERE id=?", (batch["task_id"],)).fetchone()[0])
        assert len(errors) == 6
        assert all("citation_quote_not_original" in error for error in errors)
        checkpoint = conn.execute("SELECT error_msg FROM task_checkpoints WHERE unit_key='kp-0'").fetchone()[0]
        assert "citation_quote_not_original" in checkpoint

    calls.clear()
    async def retry_model(system, prompt, **kwargs):
        payload = json.loads(prompt.split("模型输入 JSON：\n", 1)[1])
        calls.append(payload["knowledge_point_id"])
        return _response([{"citation_id": payload["source_chunks"][0]["passages"][0]["citation_id"]}], payload["knowledge_point_id"])

    monkeypatch.setattr(LLMClient, "chat_json", retry_model)
    task_id = TaskWorker.create_task("generate_recall_cards", batch["batch_id"])
    result = await RecallGenerator.execute_generation_task(task_id, batch["batch_id"])
    assert result["status"] == "completed"
    assert result["skipped_units"] == 13
    assert set(calls) == {f"kp-{index}" for index in range(6)}
    assert len(calls) == 6
    with db_session() as conn:
        assert conn.execute("SELECT count(*) FROM recall_cards").fetchone()[0] == 19
        assert conn.execute("SELECT count(*) FROM recall_card_revisions").fetchone()[0] == 19
    events = [event["event_type"] for event in _events(task_id)]
    assert events.index("validation") < events.index("saving") < events.index("save_done")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,stage,exception_type", [
    ("source_before", "source_validation", "SourceChangedError"),
    ("source_after", "source_validation", "SourceChangedError"),
    ("source_in_save", "source_validation", "SourceChangedError"),
    ("provider", "model_request", "RuntimeError"),
    ("save", "saving", "RuntimeError"),
    ("output", "output_validation", "RecallValidationError"),
])
async def test_unit_failures_persist_actual_stage_without_partial_card_writes(monkeypatch, failure, stage, exception_type):
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    batch = _seed()

    def change_source():
        with db_session() as conn:
            conn.execute("UPDATE document_chunks SET content='来源已改变'")

    async def model(system, prompt, **kwargs):
        if failure == "provider":
            raise RuntimeError("服务商连接失败")
        payload = json.loads(prompt.split("模型输入 JSON：\n", 1)[1])
        response = _response([{"citation_id": payload["source_chunks"][0]["passages"][0]["citation_id"]}])
        if failure == "source_after":
            change_source()
        if failure == "output":
            bad_card = copy.deepcopy(response["cards"][0])
            bad_card["citations"] = [{"citation_id": "private-model-body-value"}]
            response["cards"].append(bad_card)
        return response

    if failure == "source_before":
        change_source()
    if failure in ("save", "source_in_save"):
        def fail_save(**kwargs):
            if failure == "source_in_save":
                raise SourceChangedError("来源已变化")
            raise RuntimeError("数据库写入失败")
        monkeypatch.setattr(RecallGenerator, "_persist_unit", fail_save)
    monkeypatch.setattr(LLMClient, "chat_json", model)
    result = await RecallGenerator.execute_generation_task(batch["task_id"], batch["batch_id"])
    assert result["status"] == "failed"
    failures = _events(batch["task_id"], "validation_failed" if failure == "output" else "unit_failed")
    assert len(failures) == 1
    event = failures[0]
    data = json.loads(event["data_json"])
    assert event["stage"] == data["failed_stage"] == stage
    assert data["exception_type"] == exception_type
    if failure == "output":
        assert data["reason_code"] == "citation_id_unknown"
        assert data["field"] == "cards[1].citations[0].citation_id"
        assert "private-model-body-value" not in event["message"]
    with db_session() as conn:
        assert conn.execute("SELECT count(*) FROM recall_cards").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM recall_card_revisions").fetchone()[0] == 0
