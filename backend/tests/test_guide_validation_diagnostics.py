"""Chapter validation diagnostics use isolated SQLite and a fake model only."""
import copy
import json

import pytest
from fastapi import HTTPException

from app.config import settings
from app.database import db_session
from app.models.library import GuideRequest
from app.routers.materials import get_task_status
from app.services import library
from app.services.llm.observability import LLMCallError
from app.services.task_worker import TaskWorker


SECRET = "sk-private-model-output-教材机密"


@pytest.fixture
def chapter(monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    monkeypatch.setattr(settings, "LLM_API_KEY", "fake-never-network")
    monkeypatch.delenv("DEMO_MODE", raising=False)
    with db_session() as conn:
        conn.execute("INSERT INTO documents(id,filename,file_hash,file_type,status) VALUES('accounting','book.docx','hash','textbook','completed')")
        conn.execute("INSERT INTO document_chunks(id,doc_id,chunk_index,heading,content) VALUES('a1','accounting',0,'第一章 会计','会计是经济管理活动。预算控制帮助经营计划。')")
    return library.list_chapters("accounting")[0].id


def _reply():
    citation = {"chunk_id": "a1", "quote": "会计是经济管理活动。"}
    return {"summary": "基本理论", "concepts": [{"name": "会计", "explanation": "管理活动",
        "recitation": "会计是经济管理活动。", "keywords": ["管理"], "citations": [copy.deepcopy(citation)]}],
        "mind_map": {"title": "会计", "children": [{"title": "会计"}]},
        "connections": [], "citations": [copy.deepcopy(citation)]}


CASES = [
    ("not_object", "model_output_not_object", "$"),
    ("missing_summary", "schema_field_missing", "summary"),
    ("wrong_summary", "schema_field_type", "summary"),
    ("empty_summary", "schema_field_empty", "summary"),
    ("missing_concepts", "schema_field_missing", "concepts"),
    ("wrong_concepts", "schema_field_type", "concepts"),
    ("wrong_concept", "schema_field_type", "concepts[0]"),
    ("missing_recitation", "schema_field_missing", "concepts[0].recitation"),
    ("wrong_keywords", "schema_field_type", "concepts[0].keywords[0]"),
    ("wrong_mind_map", "schema_field_type", "mind_map"),
    ("unmapped_leaf", "mind_map_concept_missing", "mind_map.children[0].children[0].title"),
    ("unmapped_branch", "mind_map_concept_missing", "mind_map.children[0].title"),
    ("ambiguous_leaf", "mind_map_concept_ambiguous", "mind_map.children[0].children[0].title"),
    ("ambiguous_branch", "mind_map_concept_ambiguous", "mind_map.children[0].title"),
    ("missing_citations", "citation_missing", "citations"),
    ("wrong_citations", "citation_format", "citations"),
    ("wrong_citation", "citation_format", "citations[0]"),
    ("chunk_invalid", "citation_chunk_invalid", "citations[0].chunk_id"),
    ("chunk_outside", "citation_chunk_out_of_scope", "citations[0].chunk_id"),
    ("quote_missing", "citation_quote_missing", "citations[0].quote"),
    ("quote_not_original", "citation_quote_not_original", "citations[0].quote"),
    ("concept_quote", "citation_quote_not_original", "concepts[0].citations[0].quote"),
    ("wrong_connections", "schema_field_type", "connections"),
    ("wrong_connection", "schema_field_type", "connections[0]"),
]


def _bad(case):
    raw = _reply()
    if case == "not_object": return [SECRET]
    if case in {"missing_summary", "missing_concepts", "missing_citations"}: raw.pop(case.removeprefix("missing_"))
    elif case == "wrong_summary": raw["summary"] = {SECRET: SECRET}
    elif case == "empty_summary": raw["summary"] = ""
    elif case == "wrong_concepts": raw["concepts"] = {SECRET: SECRET}
    elif case == "wrong_concept": raw["concepts"] = [SECRET]
    elif case == "missing_recitation": raw["concepts"][0].pop("recitation")
    elif case == "wrong_keywords": raw["concepts"][0]["keywords"] = [{SECRET: SECRET}]
    elif case == "wrong_mind_map": raw["mind_map"] = SECRET
    elif case in {"unmapped_leaf", "unmapped_branch", "ambiguous_leaf", "ambiguous_branch"}:
        if case.startswith("ambiguous"):
            raw["concepts"][0]["name"] = SECRET
            raw["concepts"].append(copy.deepcopy(raw["concepts"][0]))
        raw["mind_map"]["children"] = [{"title": "分组", "children": [{"title": SECRET}]}] if case.endswith("leaf") else [{"title": SECRET}]
    elif case == "wrong_citations": raw["citations"] = {SECRET: SECRET}
    elif case == "wrong_citation": raw["citations"] = [SECRET]
    elif case == "chunk_invalid": raw["citations"][0]["chunk_id"] = {SECRET: SECRET}
    elif case == "chunk_outside": raw["citations"][0]["chunk_id"] = SECRET
    elif case == "quote_missing": raw["citations"][0].pop("quote")
    elif case == "quote_not_original": raw["citations"][0]["quote"] = SECRET
    elif case == "concept_quote": raw["concepts"][0]["citations"][0]["quote"] = SECRET
    elif case == "wrong_connections": raw["connections"] = {SECRET: SECRET}
    elif case == "wrong_connection": raw["connections"] = [SECRET]
    return raw


@pytest.mark.asyncio
@pytest.mark.parametrize("case,code,field", CASES)
async def test_invalid_model_output_has_safe_durable_diagnostics(chapter, monkeypatch, case, code, field):
    calls = []
    async def model(*args, **kwargs):
        calls.append(True)
        return _bad(case)
    monkeypatch.setattr(library.LLMClient, "chat_json", model)
    req = GuideRequest(doc_id="accounting", chapter_id=chapter)
    task = dict(library.enqueue_guide(req))
    await TaskWorker._dispatch_task(task)
    failed = get_task_status(task["id"])
    assert failed.status == "failed"
    assert field in failed.error_msg
    with db_session() as conn:
        events = [dict(row) for row in conn.execute("SELECT * FROM task_events WHERE task_id=? ORDER BY id", (task["id"],))]
    diagnostic = [event for event in events if event["event_type"] == "validation_failed"]
    assert len(diagnostic) == 1
    assert diagnostic[0]["stage"] == "validation"
    assert json.loads(diagnostic[0]["data_json"]) == {"reason_code": code, "field": field, "error_count": 1}
    assert SECRET not in json.dumps(events, ensure_ascii=False) + failed.error_msg
    assert not library.list_guides()
    assert len(calls) == 1  # Validation does not silently make another paid call.
    if case == "not_object":
        assert not any(event["event_type"] == "validation" for event in events)
        assert "输出" in failed.error_msg
    with pytest.raises(HTTPException) as direct:
        await library.generate_guide(req)
    assert direct.value.status_code == 502
    assert SECRET not in str(direct.value.detail)


@pytest.mark.asyncio
async def test_success_keeps_exact_citations_and_original_event_sequence(chapter, monkeypatch):
    async def model(*args, **kwargs): return _reply()
    monkeypatch.setattr(library.LLMClient, "chat_json", model)
    task = dict(library.enqueue_guide(GuideRequest(doc_id="accounting", chapter_id=chapter)))
    await TaskWorker._dispatch_task(task)
    assert get_task_status(task["id"]).status == "completed"
    guide = library.list_guides()[0]
    assert guide.citations[0].quote == "会计是经济管理活动。"
    with db_session() as conn:
        kinds = [row[0] for row in conn.execute("SELECT event_type FROM task_events WHERE task_id=? ORDER BY id", (task["id"],))]
    assert kinds == ["created", "reading_sources", "model_call", "validation", "saving", "completed"]


@pytest.mark.asyncio
async def test_multiple_schema_errors_report_count_without_model_values(chapter, monkeypatch):
    async def model(*args, **kwargs):
        raw = _reply()
        raw["summary"] = {SECRET: SECRET}
        raw["concepts"][0].pop("recitation")
        return raw
    monkeypatch.setattr(library.LLMClient, "chat_json", model)
    task = dict(library.enqueue_guide(GuideRequest(doc_id="accounting", chapter_id=chapter)))
    await TaskWorker._dispatch_task(task)
    with db_session() as conn:
        event = dict(conn.execute("SELECT * FROM task_events WHERE task_id=? AND event_type='validation_failed'", (task["id"],)).fetchone())
    assert json.loads(event["data_json"]) == {"reason_code": "schema_field_type", "field": "summary", "error_count": 2}
    assert SECRET not in json.dumps(event, ensure_ascii=False)


@pytest.mark.asyncio
async def test_multiple_terminal_errors_count_nodes_without_logging_names(chapter, monkeypatch):
    async def model(*args, **kwargs):
        raw = _reply()
        raw["concepts"].append(copy.deepcopy(raw["concepts"][0]))
        raw["mind_map"]["children"] = [{"title": "分组", "children": [{"title": SECRET}, {"title": "会计"}]},
                                         {"title": SECRET}]
        return raw
    monkeypatch.setattr(library.LLMClient, "chat_json", model)
    task = dict(library.enqueue_guide(GuideRequest(doc_id="accounting", chapter_id=chapter)))
    await TaskWorker._dispatch_task(task)
    assert get_task_status(task["id"]).status == "failed"
    with db_session() as conn:
        events = [dict(row) for row in conn.execute("SELECT * FROM task_events WHERE task_id=? ORDER BY id", (task["id"],))]
    diagnostic = [event for event in events if event["event_type"] == "validation_failed"]
    assert len(diagnostic) == 1
    assert json.loads(diagnostic[0]["data_json"]) == {"reason_code": "mind_map_concept_missing",
        "field": "mind_map.children[0].children[0].title", "error_count": 3}
    assert SECRET not in json.dumps(events, ensure_ascii=False)
    assert not library.list_guides()


def test_unknown_exceptions_and_cause_priority_remain_safe():
    assert SECRET not in library.guide_task_error(HTTPException(502, SECRET))
    failure = library.GuideValidationError("schema_field_missing", ("summary",))
    failure.__cause__ = LLMCallError("safe", {"service": "llm", "model": "fixture", "failed_stage": "await_headers", "exception_type": "ReadTimeout"})
    assert "ReadTimeout" in library.guide_task_error(failure)
    sanitized = library.GuideValidationError("schema_field_type", ("mind_map", SECRET, 2))
    assert sanitized.field == "mind_map.[unknown][2]"
    assert SECRET not in library.guide_task_error(sanitized)
