"""Selectable chapter evidence: fake model calls and isolated SQLite only."""
import copy
import json

import pytest

from app.config import settings
from app.database import db_session
from app.models.library import GuideRequest
from app.routers.materials import get_task_status
from app.services import library
from app.services.task_worker import TaskWorker


def _chunk(chunk_id, content, doc_id="accounting"):
    return {"id": chunk_id, "doc_id": doc_id, "doc_name": doc_id + ".docx",
            "content": content, "heading": "第一章 会计", "paragraph_start": 1,
            "paragraph_end": 2}


def test_passages_preserve_complete_whitespace_and_long_sources_once():
    original = "  会计\t基本概念。\n\n" + "长原文 " * 440 + "\n 结尾保留空格  "
    chunks = [_chunk("a1", original), _chunk("a2", "第二片段\n末尾\t")]
    context, sources = library._guide_sources(chunks)
    assert [item["chunk_id"] for item in context] == ["a1", "a2"]
    ids = []
    for source, item in zip(chunks, context):
        assert "content" not in item  # No second copy of the full source in the prompt.
        assert set(item) == {"chunk_id", "doc_id", "doc_name", "location", "passages"}
        assert "".join(p["content"] for p in item["passages"]) == source["content"]
        assert all(0 < len(p["content"]) <= 600 for p in item["passages"])
        for passage in item["passages"]:
            ids.append(passage["citation_id"])
            assert sources[passage["citation_id"]] == (source["id"], passage["content"])
    assert len(ids) == len(set(ids)) == len(sources)
    assert sum(len(p["content"]) for item in context for p in item["passages"]) == sum(len(c["content"]) for c in chunks)


def test_source_ids_stable_but_never_reused_for_changed_text_or_chunk():
    chunks = [_chunk("a1", "原文\n  保留空白。")]
    context, sources = library._guide_sources(chunks)
    assert library._guide_sources(copy.deepcopy(chunks)) == (context, sources)
    changed = library._guide_sources([_chunk("a1", "原文\n  修改正文。")])[1]
    other_chunk = library._guide_sources([_chunk("a2", chunks[0]["content"])])[1]
    assert set(sources).isdisjoint(changed)
    assert set(sources).isdisjoint(other_chunk)


@pytest.fixture
def request_scope(monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    monkeypatch.setattr(settings, "LLM_API_KEY", "fake-never-network")
    monkeypatch.delenv("DEMO_MODE", raising=False)
    with db_session() as conn:
        for doc in ("accounting", "management"):
            conn.execute("INSERT INTO documents(id,filename,file_hash,file_type,status) VALUES(?,?,?,'textbook','completed')",
                         (doc, doc + ".docx", doc))
        for chunk_id, doc, index, heading, text in (
            ("a1", "accounting", 0, "第一章 会计", "  会计是经济管理活动。\n预算控制帮助经营计划。  "),
            ("a2", "accounting", 1, "第二章 信息", "会计信息属于第二章的资料。"),
            ("m1", "management", 0, "第一章 管理", "会计是经济管理活动。预算控制帮助经营计划。"),
        ):
            conn.execute("INSERT INTO document_chunks(id,doc_id,chunk_index,heading,content) VALUES(?,?,?,?,?)",
                         (chunk_id, doc, index, heading, text))
    return GuideRequest(doc_id="accounting", chapter_id=library.list_chapters("accounting")[0].id,
                        management_doc_ids=["management"])


def _reply(prompt):
    data = json.loads(prompt)
    accounting = data["complete_accounting_chapter"][0]["passages"][0]["citation_id"]
    management = data["management_excerpts"][0]["passages"][0]["citation_id"]
    return {"summary": "会计基本理论", "concepts": [{"name": "会计", "explanation": "经济管理活动",
        "recitation": "会计是经济管理活动。", "keywords": ["管理"], "citations": [{"citation_id": accounting}]}],
        "mind_map": {"title": "会计", "children": [{"title": "管理活动"}]},
        "connections": [{"accounting_concept": "会计", "management_concept": "预算控制",
                         "explanation": "共同支持经营计划", "application": "通过预算控制经营活动",
                         "evidence_level": "textbook_supported",
                         "citations": [{"citation_id": accounting}, {"citation_id": management}]}],
        "citations": [{"citation_id": accounting}]}


@pytest.mark.asyncio
async def test_generate_resolves_ids_to_canonical_saved_citations(request_scope, monkeypatch):
    prompts = []
    async def model(system, prompt, **kwargs):
        prompts.append(json.loads(prompt))
        assert "citation_id" in system
        raw = _reply(prompt)
        # Untrusted metadata must still be derived from the selected source.
        raw["citations"][0].update(doc_id="spoof", doc_name="spoof", location="第999页")
        return raw
    monkeypatch.setattr(library.LLMClient, "chat_json", model)
    guide = await library.generate_guide(request_scope)
    assert len(prompts) == 1
    assert [c["chunk_id"] for c in prompts[0]["complete_accounting_chapter"]] == ["a1"]
    assert [c["chunk_id"] for c in prompts[0]["management_excerpts"]] == ["m1"]
    assert guide.connections[0].evidence_level == "textbook_supported"
    citations = [*guide.citations, *guide.concepts[0].citations, *guide.connections[0].citations]
    with db_session() as conn:
        originals = dict(conn.execute("SELECT id,content FROM document_chunks"))
        payload = json.loads(conn.execute("SELECT payload_json FROM library_guides WHERE id=?", (guide.id,)).fetchone()[0])
    for citation in citations:
        assert citation.quote == originals[citation.chunk_id].strip()
        assert citation.quote in originals[citation.chunk_id]
        assert citation.doc_id in {"accounting", "management"}
        assert citation.doc_name == citation.doc_id + ".docx"
        assert citation.location != "第999页"
        assert set(citation.model_dump()) == {"chunk_id", "doc_id", "doc_name", "location", "quote"}
    assert "citation_id" not in json.dumps(payload)
    assert library.get_guide(guide.id) == guide


FAILURES = [
    ("unknown", "citation_id_unknown", "citations[0].citation_id"),
    ("invalid_list", "citation_id_invalid", "citations[0].citation_id"),
    ("invalid_null", "citation_id_invalid", "citations[0].citation_id"),
    ("invalid_empty", "citation_id_invalid", "citations[0].citation_id"),
    ("management_summary", "citation_chunk_out_of_scope", "citations[0].chunk_id"),
    ("management_concept", "citation_chunk_out_of_scope", "concepts[0].citations[0].chunk_id"),
    ("other_chapter", "citation_id_unknown", "citations[0].citation_id"),
    ("old_source", "citation_id_unknown", "citations[0].citation_id"),
    ("conflicting_chunk", "citation_reference_conflict", "citations[0].chunk_id"),
    ("conflicting_quote", "citation_reference_conflict", "citations[0].quote"),
]


def _bad_reply(prompt, case, old_id):
    raw = _reply(prompt)
    data = json.loads(prompt)
    citation = raw["citations"][0]
    if case == "unknown": citation["citation_id"] = "src_nonexistent_private_model_value"
    elif case == "invalid_list": citation["citation_id"] = ["unhashable-private-model-value"]
    elif case == "invalid_null": citation["citation_id"] = None
    elif case == "invalid_empty": citation["citation_id"] = ""
    elif case in {"management_summary", "management_concept"}:
        selected = {"citation_id": data["management_excerpts"][0]["passages"][0]["citation_id"]}
        if case == "management_summary": raw["citations"] = [selected]
        else: raw["concepts"][0]["citations"] = [selected]
    elif case in {"other_chapter", "old_source"}: citation["citation_id"] = old_id
    elif case == "conflicting_chunk": citation["chunk_id"] = "m1"
    elif case == "conflicting_quote": citation["quote"] = "private-model-paraphrase"
    return raw


@pytest.mark.asyncio
@pytest.mark.parametrize("case,code,field", FAILURES)
async def test_invalid_ids_fail_once_with_durable_safe_diagnostics(request_scope, monkeypatch, case, code, field):
    # An ID from another chapter/request must not resolve in the current input map.
    old_id = None
    if case in {"other_chapter", "old_source"}:
        with db_session() as conn:
            chunk_id = "a2" if case == "other_chapter" else "a1"
            chunk = dict(conn.execute("SELECT *, 'accounting.docx' AS doc_name FROM document_chunks WHERE id=?", (chunk_id,)).fetchone())
            old_id = next(iter(library._guide_sources([chunk])[1]))
            if case == "old_source":
                conn.execute("UPDATE document_chunks SET content='会计更新为当前教材内容。预算控制。' WHERE id='a1'")
    calls = []
    async def model(system, prompt, **kwargs):
        calls.append(True)
        return _bad_reply(prompt, case, old_id)
    monkeypatch.setattr(library.LLMClient, "chat_json", model)
    task = dict(library.enqueue_guide(request_scope))
    await TaskWorker._dispatch_task(task)
    failed = get_task_status(task["id"])
    assert failed.status == "failed"
    assert field in failed.error_msg and code in failed.error_msg
    assert len(calls) == 1  # Invalid evidence never triggers another model call.
    assert library.list_guides() == []
    with db_session() as conn:
        events = [dict(row) for row in conn.execute("SELECT * FROM task_events WHERE task_id=? ORDER BY id", (task["id"],))]
    diagnostics = [e for e in events if e["event_type"] == "validation_failed"]
    assert len(diagnostics) == 1
    assert diagnostics[0]["stage"] == "validation"
    assert json.loads(diagnostics[0]["data_json"]) == {"reason_code": code, "field": field, "error_count": 1}
    assert not any(e["event_type"] in {"saving", "completed"} for e in events)
    assert "private-model" not in json.dumps(events, ensure_ascii=False) + failed.error_msg


def test_matching_mixed_protocol_is_accepted_but_legacy_quotes_remain_strict():
    chunk = _chunk("a1", " 原文\n保持空白。 ")
    _, sources = library._guide_sources([chunk])
    citation_id = next(iter(sources))
    mixed = {"citation_id": citation_id, "chunk_id": "a1", "quote": chunk["content"]}
    resolved = library._citations([mixed], {"a1": chunk}, required=True, field=("citations",), sources=sources)
    assert resolved[0]["quote"] == chunk["content"]
    legacy = {"chunk_id": "a1", "quote": "原文\n保持空白。"}
    assert library._citations([legacy], {"a1": chunk}, required=True, field=("citations",), sources=sources)[0]["quote"] == legacy["quote"]
    legacy["quote"] = "原文 保持空白。"
    with pytest.raises(library.GuideValidationError) as rejected:
        library._citations([legacy], {"a1": chunk}, required=True, field=("citations",), sources=sources)
    assert rejected.value.reason_code == "citation_quote_not_original"
