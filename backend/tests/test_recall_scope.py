"""Textbook-driven recall generation, isolated SQLite and fake provider only."""

import json

import pytest

from app.config import settings
from app.database import db_session
from app.services import recall_scope
from app.services.recall_cards import RecallCardsService
from app.services.recall_generator import RecallGenerationError, RecallGenerator, SourceChangedError, _sha256_text


def _book(doc_id="accounting", title="会计学", chapters=("第一章 会计概述", "第二章 会计要素")):
    with db_session() as conn:
        conn.execute("INSERT INTO documents (id,filename,file_hash,file_type,status) VALUES (?,?,?,'textbook','completed')",
                     (doc_id, title + ".pdf", doc_id + "-hash"))
        for i, heading in enumerate(chapters):
            conn.execute("INSERT INTO document_chunks (id,doc_id,chunk_index,heading,content,embedding) VALUES (?,?,?,?,?,?)",
                         (f"{doc_id}-{i}", doc_id, i, heading, f"原文{i}：资产是企业拥有或者控制的资源。", b"embedding"))
    return doc_id


def _kp(kp_id="kp-1", chunks=("accounting-0",), *, reviewed=False, name="资产", chapter="第三节 采购业务"):
    with db_session() as conn:
        conn.execute("INSERT INTO knowledge_points (id,name,chapter,source_reference) VALUES (?,?,?,?)",
                     (kp_id, name, chapter, "教材 P1" if reviewed else json.dumps({"chunk_ids": list(chunks)})))
        if reviewed:
            citations = []
            for chunk_id in chunks:
                chunk = conn.execute("SELECT * FROM document_chunks WHERE id=?", (chunk_id,)).fetchone()
                citations.append({"chunk_id": chunk_id, "doc_id": chunk["doc_id"],
                                  "quote": "资产是企业拥有或者控制的资源", "source_hash": _sha256_text(chunk["content"])})
            conn.execute("INSERT OR IGNORE INTO extraction_batches (id,doc_ids_json,status) VALUES ('extract','[]','completed')")
            conn.execute("INSERT INTO extraction_candidates (id,extraction_id,name,citations_json,status,target_kp_id) VALUES (?,'extract',?,?,'merged',?)",
                         ("candidate-" + kp_id, name, json.dumps(citations), kp_id))


def _generate(p=None, *, request_id="request-1", instructions=""):
    p = p or recall_scope.preview("accounting")
    return recall_scope.generate(p["doc_id"], p["library_chapter_id"], request_id=request_id,
                                 preview_fingerprint=p["fingerprint"], instructions=instructions)


def _counts():
    with db_session() as conn:
        return tuple(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                     for table in ("recall_chapters", "recall_generation_batches", "tasks", "recall_cards"))


def _snapshot(generation):
    with db_session() as conn:
        return json.loads(conn.execute("SELECT input_snapshot_json FROM recall_generation_batches WHERE id=?",
                                       (generation["batch_id"],)).fetchone()[0])


def test_preview_is_read_only_and_ignores_subject_and_section_title_guesses():
    _book()
    _book("management", "管理学", ("第一章 管理",))
    _kp(reviewed=True)
    _kp("foreign", ("management-0",), name="管理职能", chapter="第一章 会计概述")
    RecallCardsService.create_chapter({"title": "管理学"})
    before = _counts()
    p = recall_scope.preview("accounting")
    assert p["eligible_count"] == 1
    assert p["knowledge_points"][0]["id"] == "kp-1"
    assert p["knowledge_points"][0]["chapter_title"] == "第一章 会计概述"
    assert _counts() == before
    result = _generate(p)
    assert result["batch_count"] == 1
    with db_session() as conn:
        directories = [dict(r) for r in conn.execute("SELECT title,source_doc_id FROM recall_chapters ORDER BY source_doc_id")]
    assert directories == [{"title": "管理学", "source_doc_id": None},
                           {"title": "第一章 会计概述", "source_doc_id": "accounting"}]


def test_merged_cross_book_and_chapter_uses_only_selected_canonical_sources():
    _book()
    _book("management", "管理学", ("第一章 管理",))
    _kp(chunks=("accounting-0", "accounting-1", "management-0"), reviewed=True)
    with db_session() as conn:
        row = conn.execute("SELECT citations_json FROM extraction_candidates").fetchone()
        refs = json.loads(row[0])
        refs[2]["source_hash"] = "stale-foreign-book-does-not-poison-selected-book"
        conn.execute("UPDATE extraction_candidates SET citations_json=?", (json.dumps(refs),))
    p = recall_scope.preview("accounting")
    assert p["eligible_count"] == 2
    assert p["batch_count"] == 2
    result = _generate(p)
    for i, generation in enumerate(result["generations"]):
        snapshot = _snapshot(generation)
        assert [c["id"] for c in snapshot["units"][0]["chunks"]] == [f"accounting-{i}"]
        assert snapshot["units"][0]["library_chapter_id"] == p["chapters"][i]["library_chapter_id"]
    specific = recall_scope.preview("accounting", p["chapters"][1]["library_chapter_id"])
    assert specific["eligible_count"] == 1


@pytest.mark.parametrize("change,reason", [
    ({"quote": "虚构摘录"}, "引用摘录"), ({"source_hash": "old"}, "原文已变化"),
    ({"doc_id": "wrong"}, "资料归属"),
])
def test_bad_selected_citation_is_reported_and_not_generated(change, reason):
    _book()
    _kp(reviewed=True)
    with db_session() as conn:
        refs = json.loads(conn.execute("SELECT citations_json FROM extraction_candidates").fetchone()[0])
        refs[0].update(change)
        conn.execute("UPDATE extraction_candidates SET citations_json=?", (json.dumps(refs),))
    p = recall_scope.preview("accounting")
    assert p["eligible_count"] == 0
    assert reason in p["skipped"][0]["reason"]
    with pytest.raises(RecallGenerationError, match="没有可生成"):
        _generate(p)
    assert _counts() == (0, 0, 0, 0)


def test_display_reference_never_uses_chapter_title_fallback():
    _book()
    _kp(chunks=())
    with db_session() as conn:
        conn.execute("UPDATE knowledge_points SET chapter='第一章 会计概述',source_reference='会计学.pdf P1'")
    p = recall_scope.preview("accounting")
    assert p["eligible_count"] == 0
    assert p["skipped_count"] == 1
    assert "暂不能确定教材归属" in p["skipped"][0]["reason"]


def test_more_than_twenty_points_are_split_without_truncation_and_replayed():
    _book(chapters=("第一章 会计概述",))
    for i in range(23):
        _kp(f"kp-{i:02d}")
    p = recall_scope.preview("accounting")
    assert (p["eligible_count"], p["batch_count"]) == (23, 2)
    first = _generate(p)
    second = _generate(p)
    assert first == second
    assert [len(g["knowledge_point_ids"]) for g in first["generations"]] == [20, 3]
    assert _counts() == (1, 2, 2, 0)
    with pytest.raises(RecallGenerationError, match="不同的教材生成请求"):
        _generate(p, instructions="另一种要求")


@pytest.mark.parametrize("mutation", ["content", "heading", "reference", "name", "document"])
def test_preview_change_rejects_without_partial_writes(mutation):
    _book()
    _kp(reviewed=True)
    p = recall_scope.preview("accounting")
    with db_session() as conn:
        if mutation == "content":
            conn.execute("UPDATE document_chunks SET content='changed' WHERE id='accounting-0'")
        elif mutation == "heading":
            conn.execute("UPDATE document_chunks SET heading='第二章 新归属' WHERE id='accounting-0'")
        elif mutation == "reference":
            conn.execute("UPDATE extraction_candidates SET citations_json='[]'")
        elif mutation == "name":
            conn.execute("UPDATE knowledge_points SET name='新名称'")
        else:
            conn.execute("UPDATE documents SET file_hash='changed' WHERE id='accounting'")
    with pytest.raises(RecallGenerationError, match="已变化"):
        _generate(p)
    assert _counts() == (0, 0, 0, 0)


def test_failure_after_first_batch_rolls_back_all_directories_batches_and_tasks(monkeypatch):
    _book()
    _kp(chunks=("accounting-0", "accounting-1"), reviewed=True)
    original = RecallGenerator.create_generation_batch
    calls = []

    def fail_second(*args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            raise RecallGenerationError("模拟第二批失败")
        return original(*args, **kwargs)

    monkeypatch.setattr(RecallGenerator, "create_generation_batch", fail_second)
    with pytest.raises(RecallGenerationError, match="第二批失败"):
        _generate()
    assert _counts() == (0, 0, 0, 0)


def test_archived_source_directory_is_reported_without_restoring_it():
    _book()
    _kp()
    chapter = RecallCardsService.create_chapter({"title": "第一章 会计概述", "source_doc_id": "accounting"})
    RecallCardsService.archive_chapter(chapter["id"])
    p = recall_scope.preview("accounting")
    assert p["eligible_count"] == 0
    assert "已归档" in p["skipped"][0]["reason"]
    assert _counts() == (1, 0, 0, 0)


@pytest.mark.parametrize("kind", ["chunks", "characters"])
def test_oversized_sources_are_explicitly_skipped(kind):
    _book(chapters=("第一章 会计概述",))
    chunks = ["accounting-0"]
    with db_session() as conn:
        if kind == "chunks":
            for i in range(1, 11):
                conn.execute("INSERT INTO document_chunks (id,doc_id,chunk_index,heading,content) VALUES (?,'accounting',?,'第一节 内容','原文')",
                             (f"accounting-{i}", i))
                chunks.append(f"accounting-{i}")
        else:
            conn.execute("UPDATE document_chunks SET content=?", ("正文" * 6001,))
    _kp(chunks=chunks)
    p = recall_scope.preview("accounting")
    assert p["eligible_count"] == 0
    assert "超过" in p["skipped"][0]["reason"]
    assert _counts() == (0, 0, 0, 0)


def test_scoped_generator_revalidates_membership_and_ignores_injected_chunks():
    _book()
    _kp()
    _kp("wrong", ("accounting-1",))
    p = recall_scope.preview("accounting")
    directory = RecallCardsService.create_chapter({"title": "第一章 会计概述", "source_doc_id": "accounting"})
    with pytest.raises(RecallGenerationError, match="没有属于"):
        RecallGenerator.create_generation_batch(directory["id"], units=[{"knowledge_point_id": "wrong", "chunks": [{"id": "accounting-0"}]}],
            library_chapter_id=p["chapters"][0]["library_chapter_id"], source_doc_id="accounting")
    assert _counts() == (1, 0, 0, 0)


def test_snapshot_rejects_canonical_membership_change_even_when_cited_text_is_unchanged():
    _book()
    _kp(chunks=("accounting-1",))
    result = _generate()
    unit = RecallGenerator._load_snapshot_units(_snapshot(result["generations"][0]))[0]
    RecallGenerator._assert_snapshot_current(unit)
    with db_session() as conn:
        conn.execute("UPDATE document_chunks SET heading='第一节 会计要素' WHERE id='accounting-1'")
    with pytest.raises(SourceChangedError, match="章节|变化"):
        RecallGenerator._assert_snapshot_current(unit)


@pytest.mark.asyncio
async def test_instructions_reach_real_worker_prompt_without_becoming_task_log_title(monkeypatch):
    _book()
    _kp()
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    instructions = "请生成可背诵的短段，并重点强调概念。"
    result = _generate(instructions=instructions)
    generation = result["generations"][0]
    snapshot = _snapshot(generation)
    assert snapshot["instructions"] == instructions
    with db_session() as conn:
        assert instructions not in conn.execute("SELECT message FROM tasks WHERE id=?", (generation["task_id"],)).fetchone()[0]
    prompts = []

    async def fake_model(system, prompt, **kwargs):
        assert "在原文证据范围内遵循 instructions" in prompt
        payload = json.loads(prompt.split("模型输入 JSON：\n", 1)[1])
        prompts.append(payload)
        return {"knowledge_point_id": "kp-1", "cards": [{"title": "资产", "card_type": "definition",
            "front_content": "什么是资产？", "back_content": "资产是企业拥有或者控制的资源。",
            "citations": [{"chunk_id": "accounting-0", "source_hash": "accounting-hash", "quote": "资产是企业拥有或者控制的资源"}]}], "insufficient_reason": None}

    monkeypatch.setattr("app.services.recall_generator.LLMClient.chat_json", fake_model)
    validations = []
    original_validation = RecallGenerator._assert_snapshot_current

    def check_write_transaction(unit, conn=None):
        if conn is not None:
            validations.append(conn.in_transaction)
        return original_validation(unit, conn=conn)

    monkeypatch.setattr(RecallGenerator, "_assert_snapshot_current", check_write_transaction)
    outcome = await RecallGenerator.execute_generation_task(generation["task_id"], generation["batch_id"])
    assert outcome["status"] == "completed"
    assert prompts[0]["instructions"] == instructions
    assert validations == [True]
    with db_session() as conn:
        card = conn.execute("SELECT status,is_confirmed,is_ai_generated FROM recall_cards").fetchone()
        assert tuple(card) == ("draft", 0, 1)


def test_unreviewed_extraction_is_not_silently_accepted():
    _book()
    _kp(reviewed=True)
    with db_session() as conn:
        conn.execute("UPDATE extraction_candidates SET status='pending'")
    p = recall_scope.preview("accounting")
    assert p["eligible_count"] == 0
    assert _counts() == (0, 0, 0, 0)


def test_request_id_is_global_across_scopes_and_changed_sources_block_replay():
    _book()
    _kp(chunks=("accounting-0", "accounting-1"))
    p = recall_scope.preview("accounting")
    _generate(p)
    other = recall_scope.preview("accounting", p["chapters"][0]["library_chapter_id"])
    with pytest.raises(RecallGenerationError, match="不同的教材生成请求"):
        _generate(other)
    with db_session() as conn:
        conn.execute("UPDATE documents SET file_hash='changed'")
    with pytest.raises(RecallGenerationError, match="已变化"):
        _generate(p)


def test_missing_request_id_or_fingerprint_is_rejected():
    _book()
    _kp()
    with pytest.raises(RecallGenerationError, match="request_id"):
        recall_scope.generate("accounting", request_id="", preview_fingerprint="x")
    with pytest.raises(RecallGenerationError, match="预览指纹"):
        recall_scope.generate("accounting", request_id="x", preview_fingerprint="")


def test_embedding_and_index_status_changes_do_not_expire_source_preview():
    _book()
    _kp()
    p = recall_scope.preview("accounting")
    with db_session() as conn:
        conn.execute("UPDATE document_chunks SET embedding=?,embedding_status='completed',updated_at=CURRENT_TIMESTAMP", (b"new-index",))
    assert recall_scope.preview("accounting")["fingerprint"] == p["fingerprint"]
    assert _generate(p)["batch_count"] == 1


def test_missing_manual_source_is_reported_instead_of_silently_disappearing():
    _book()
    _kp(chunks=("deleted-chunk",))
    p = recall_scope.preview("accounting")
    assert p["eligible_count"] == 0
    assert p["skipped_count"] == 1
    assert "切片已缺失" in p["skipped"][0]["reason"]


def test_deleted_document_becomes_source_changed_for_worker():
    _book()
    _kp()
    result = _generate()
    unit = RecallGenerator._load_snapshot_units(_snapshot(result["generations"][0]))[0]
    with db_session() as conn:
        # Keep the queued snapshot but simulate a disappeared source document.
        conn.execute("DELETE FROM document_chunks")
        conn.execute("DELETE FROM documents")
    with pytest.raises(SourceChangedError):
        RecallGenerator._assert_snapshot_current(unit)


def test_normalized_existing_source_directory_is_reused_without_renaming():
    _book()
    _kp()
    chapter = RecallCardsService.create_chapter({"title": "第一章  会计概述", "source_doc_id": "accounting"})
    result = _generate()
    assert result["generations"][0]["chapter_id"] == chapter["id"]
    assert _counts() == (1, 1, 1, 0)
    unit = RecallGenerator._load_snapshot_units(_snapshot(result["generations"][0]))[0]
    RecallGenerator._assert_snapshot_current(unit)


@pytest.mark.asyncio
async def test_source_membership_change_during_model_call_prevents_card_write(monkeypatch):
    _book()
    _kp()
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    result = _generate()
    generation = result["generations"][0]

    async def changing_model(system, prompt, **kwargs):
        with db_session() as conn:
            conn.execute("UPDATE document_chunks SET heading='第一章 新章节' WHERE id='accounting-0'")
        return {"knowledge_point_id": "kp-1", "cards": [{"title": "资产", "card_type": "definition",
            "front_content": "什么是资产？", "back_content": "资产是企业拥有或者控制的资源。",
            "citations": [{"chunk_id": "accounting-0", "source_hash": "accounting-hash", "quote": "资产是企业拥有或者控制的资源"}]}], "insufficient_reason": None}

    monkeypatch.setattr("app.services.recall_generator.LLMClient.chat_json", changing_model)
    outcome = await RecallGenerator.execute_generation_task(generation["task_id"], generation["batch_id"])
    assert outcome["status"] in ("failed", "partial")
    assert _counts()[3] == 0


@pytest.mark.parametrize("mutation", ["rebind", "archive", "rename", "delete"])
@pytest.mark.asyncio
async def test_directory_change_during_model_call_prevents_card_write(monkeypatch, mutation):
    _book()
    _book("management", "管理学", ("第一章 管理",))
    _kp()
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    generation = _generate()["generations"][0]

    async def changing_model(system, prompt, **kwargs):
        with db_session() as conn:
            if mutation == "rebind":
                conn.execute("UPDATE recall_chapters SET source_doc_id='management'")
            elif mutation == "archive":
                conn.execute("UPDATE recall_chapters SET status='archived'")
            elif mutation == "rename":
                conn.execute("UPDATE recall_chapters SET title='另一目录'")
            else:
                conn.execute("DELETE FROM recall_chapters")
        return {"knowledge_point_id": "kp-1", "cards": [{"title": "资产", "card_type": "definition",
            "front_content": "什么是资产？", "back_content": "资产是企业拥有或者控制的资源。",
            "citations": [{"chunk_id": "accounting-0", "source_hash": "accounting-hash", "quote": "资产是企业拥有或者控制的资源"}]}], "insufficient_reason": None}

    monkeypatch.setattr("app.services.recall_generator.LLMClient.chat_json", changing_model)
    outcome = await RecallGenerator.execute_generation_task(generation["task_id"], generation["batch_id"])
    assert outcome["status"] in ("failed", "partial")
    assert _counts()[3] == 0


@pytest.mark.parametrize("field,value", [("page_number", 42), ("paragraph_ref", "段落 80")])
def test_source_location_change_invalidates_snapshot(field, value):
    _book()
    _kp()
    unit = RecallGenerator._load_snapshot_units(_snapshot(_generate()["generations"][0]))[0]
    with db_session() as conn:
        conn.execute(f"UPDATE document_chunks SET {field}=? WHERE id='accounting-0'", (value,))
    with pytest.raises(SourceChangedError, match="变化"):
        RecallGenerator._assert_snapshot_current(unit)


@pytest.mark.parametrize("invalid", ["empty", "parse_error"])
@pytest.mark.parametrize("reviewed", [False, True])
@pytest.mark.parametrize("whole_book", [False, True])
def test_unusable_cited_chunk_is_reported_for_its_scope(invalid, reviewed, whole_book):
    _book(chapters=("第一章 会计概述",))
    with db_session() as conn:
        conn.execute("INSERT INTO document_chunks (id,doc_id,chunk_index,heading,content) VALUES ('bad-chunk','accounting',1,'第一节 资产','资产是企业拥有或者控制的资源。')")
    _kp(chunks=("bad-chunk",), reviewed=reviewed)
    chapter_id = recall_scope.preview("accounting")["chapters"][0]["library_chapter_id"]
    with db_session() as conn:
        if invalid == "empty":
            conn.execute("UPDATE document_chunks SET content='   ' WHERE id='bad-chunk'")
        else:
            conn.execute("UPDATE document_chunks SET parse_error='OCR失败' WHERE id='bad-chunk'")
    p = recall_scope.preview("accounting", None if whole_book else chapter_id)
    assert p["eligible_count"] == 0
    assert p["skipped_count"] == 1
    assert p["skipped"][0]["knowledge_point_id"] == "kp-1"
    assert "原文不可用" in p["skipped"][0]["reason"]


def test_unusable_foreign_chapter_does_not_poison_valid_selected_scope():
    _book()
    _kp(chunks=("accounting-0", "accounting-1"), reviewed=True)
    first_id = recall_scope.preview("accounting")["chapters"][0]["library_chapter_id"]
    with db_session() as conn:
        conn.execute("UPDATE document_chunks SET parse_error='OCR失败' WHERE id='accounting-1'")
    specific = recall_scope.preview("accounting", first_id)
    assert specific["eligible_count"] == 1
    assert specific["skipped_count"] == 0
    whole = recall_scope.preview("accounting")
    assert whole["eligible_count"] == 1
    assert whole["skipped_count"] == 1
    assert "原文不可用" in whole["skipped"][0]["reason"]
