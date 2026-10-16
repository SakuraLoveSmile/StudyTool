"""Focused tests for Phase E grounded recall generation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.database import db_session
from app.services.llm.client import LLMClient
from app.services.recall_generator import (
    RecallGenerationError,
    RecallGenerator,
    SourceChangedError,
)
from app.services.task_worker import TaskWorker


def _seed(chapter_id="ch-1", kp_ids=("kp-1",), content="管理是协调资源实现目标的过程"):
    doc_id = "doc-1"
    rows = []
    with db_session() as conn:
        conn.execute(
            "INSERT INTO documents (id, filename, file_hash, file_type, status) VALUES (?, ?, ?, 'textbook', 'completed')",
            (doc_id, "教材.txt", "doc-hash-1"),
        )
        conn.execute(
            "INSERT INTO recall_chapters (id, source_doc_id, source_doc_name_snapshot, title, normalized_title, title_normalized) VALUES (?, ?, ?, ?, ?, ?)",
            (chapter_id, doc_id, "教材.txt", "第一章 管理", "第一章 管理", "第一章 管理"),
        )
        for i, kp_id in enumerate(kp_ids):
            chunk_id = f"chunk-{i + 1}"
            kp_content = content if i == 0 else f"{content}-{i}"
            conn.execute(
                "INSERT INTO document_chunks (id, doc_id, chunk_index, content, parse_error) VALUES (?, ?, ?, ?, NULL)",
                (chunk_id, doc_id, i, kp_content),
            )
            conn.execute(
                "INSERT INTO knowledge_points (id, name, chapter, is_deleted, source_reference) VALUES (?, ?, ?, 0, ?)",
                (kp_id, f"知识点{i + 1}", "第一章 管理", json.dumps({"chunk_ids": [chunk_id]})),
            )
            rows.append((kp_id, chunk_id, kp_content))
    return doc_id, rows


def _unit(kp_id, chunk_id, content, *, source_hash="doc-hash-1"):
    return {
        "knowledge_point_id": kp_id,
        "chapter_id": "ch-1",
        "chunks": [
            {
                "id": chunk_id,
                "doc_id": "doc-1",
                "content": content,
                "source_hash": source_hash,
            }
        ],
    }


def _response(kp_id, chunk_id, quote, source_hash="doc-hash-1"):
    return {
        "knowledge_point_id": kp_id,
        "cards": [
            {
                "title": "定义卡",
                "card_type": "definition",
                "front_content": "什么是管理？",
                "back_content": "管理是协调资源实现目标的过程。",
                "citations": [
                    {
                        "chunk_id": chunk_id,
                        "quote": quote,
                        "source_hash": source_hash,
                    }
                ],
            }
        ],
        "insufficient_reason": None,
    }


def _make_batch(*, kp_ids=("kp-1",), request_id="req-1", mode="formal"):
    _, rows = _seed(kp_ids=kp_ids)
    units = [_unit(kp_id, chunk_id, content) for kp_id, chunk_id, content in rows]
    return RecallGenerator.create_generation_batch(
        "ch-1", units, request_id=request_id, mode=mode
    )


@pytest.mark.asyncio
async def test_success_persists_pending_candidates_and_snapshot(monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    _, rows = _seed()
    batch = RecallGenerator.create_generation_batch(
        "ch-1", [_unit(*rows[0])], request_id="request-success"
    )
    monkeypatch.setattr(
        LLMClient,
        "chat_json",
        AsyncMock(return_value=_response("kp-1", "chunk-1", rows[0][2])),
    )
    result = await RecallGenerator.execute_generation_task(batch["task_id"], batch["batch_id"])
    assert result["status"] == "completed"
    with db_session() as conn:
        card = conn.execute("SELECT * FROM recall_cards").fetchone()
        revision = conn.execute("SELECT * FROM recall_card_revisions").fetchone()
        persisted_batch = conn.execute(
            "SELECT request_id, request_fingerprint, input_snapshot_json FROM recall_generation_batches WHERE id = ?",
            (batch["batch_id"],),
        ).fetchone()
    assert card["candidate"] == 1
    assert card["is_confirmed"] == 0
    assert card["is_ai_generated"] == 1
    assert card["publication_status"] == "draft"
    assert revision["review_status"] == "draft"
    assert persisted_batch["request_id"] == "request-success"
    assert json.loads(persisted_batch["input_snapshot_json"])["units"]


@pytest.mark.asyncio
@pytest.mark.parametrize("padding", [" \n", "\r\n\t", "\u3000\u00a0"])
@pytest.mark.parametrize("retry_existing", [False, True])
async def test_snapshot_preserves_original_whitespace_through_worker(monkeypatch, padding, retry_existing):
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    original = padding + "管理是协调资源实现目标的过程" + padding
    _, rows = _seed(content=original)
    batch = RecallGenerator.create_generation_batch("ch-1", [_unit(*rows[0])])
    with db_session() as conn:
        raw = conn.execute("SELECT input_snapshot_json FROM recall_generation_batches WHERE id=?", (batch["batch_id"],)).fetchone()[0]
    snapshot = json.loads(raw)
    chunk = snapshot["units"][0]["chunks"][0]
    assert chunk["content"] == original
    assert chunk["content_hash"] == hashlib.sha256(original.encode()).hexdigest()
    unit = RecallGenerator._load_snapshot_units(snapshot)[0]
    assert unit.chunks[0].content == original

    task_id = batch["task_id"]
    if retry_existing:
        # A pre-fix failed batch already contains the correct original text and
        # hash. Retrying must consume that snapshot without rewriting it.
        with db_session() as conn:
            conn.execute("UPDATE recall_generation_batches SET status='failed' WHERE id=?", (batch["batch_id"],))
        task_id = TaskWorker.create_task("generate_recall_cards", batch["batch_id"])

    async def model(system, prompt, **kwargs):
        payload = json.loads(prompt.split("模型输入 JSON：\n", 1)[1])
        assert "".join(p["content"] for p in payload["source_chunks"][0]["passages"]) == original
        return _response("kp-1", "chunk-1", original.strip())

    monkeypatch.setattr(LLMClient, "chat_json", model)
    result = await RecallGenerator.execute_generation_task(task_id, batch["batch_id"])
    assert result["status"] == "completed", result
    with db_session() as conn:
        assert conn.execute("SELECT count(*) FROM recall_cards").fetchone()[0] == 1
        assert conn.execute("SELECT input_snapshot_json FROM recall_generation_batches WHERE id=?", (batch["batch_id"],)).fetchone()[0] == raw


def test_snapshot_whitespace_tampering_is_still_rejected():
    _, rows = _seed(content="\n管理是协调资源实现目标的过程\n")
    batch = RecallGenerator.create_generation_batch("ch-1", [_unit(*rows[0])])
    with db_session() as conn:
        snapshot = json.loads(conn.execute("SELECT input_snapshot_json FROM recall_generation_batches WHERE id=?", (batch["batch_id"],)).fetchone()[0])
    snapshot["units"][0]["chunks"][0]["content"] += " "
    with pytest.raises(RecallGenerationError, match="哈希不匹配"):
        RecallGenerator._load_snapshot_units(snapshot)


def test_invalid_refs_and_quotes_are_rejected_without_partial_unit():
    _, rows = _seed()
    unit = RecallGenerator._unit_from_snapshot(
        {
            "knowledge_point_id": "kp-1",
            "knowledge_point_name": "知识点1",
            "chapter_id": "ch-1",
            "chapter_title": "第一章 管理",
            "chunks": [
                {
                    "id": "chunk-1",
                    "doc_id": "doc-1",
                    "doc_name": "教材.txt",
                    "source_hash": "doc-hash-1",
                    "content": rows[0][2],
                }
            ],
        }
    )
    bad_ref = _response("kp-1", "not-in-snapshot", rows[0][2])
    with pytest.raises(RecallGenerationError):
        RecallGenerator.validate_model_output(bad_ref, unit)
    bad_quote = _response("kp-1", "chunk-1", "模型编造的原文")
    with pytest.raises(RecallGenerationError):
        RecallGenerator.validate_model_output(bad_quote, unit)


@pytest.mark.asyncio
async def test_partial_batch_keeps_success_and_reports_failed_unit(monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    _, rows = _seed(kp_ids=("kp-1", "kp-2"))
    batch = RecallGenerator.create_generation_batch(
        "ch-1", [_unit(*rows[0]), _unit(*rows[1])], request_id="request-partial"
    )
    responses = [
        _response("kp-1", "chunk-1", rows[0][2]),
        {"knowledge_point_id": "kp-2", "cards": [{"title": "坏卡"}]},
    ]
    monkeypatch.setattr(LLMClient, "chat_json", AsyncMock(side_effect=responses))
    result = await RecallGenerator.execute_generation_task(batch["task_id"], batch["batch_id"])
    assert result["status"] == "partial"
    with db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM recall_cards").fetchone()[0] == 1
        statuses = conn.execute(
            "SELECT unit_key, status FROM task_checkpoints ORDER BY unit_key"
        ).fetchall()
    assert {row["unit_key"]: row["status"] for row in statuses} == {
        "kp-1": "completed",
        "kp-2": "failed",
    }


@pytest.mark.asyncio
async def test_retry_skips_completed_knowledge_point(monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    _, rows = _seed(kp_ids=("kp-1", "kp-2"))
    batch = RecallGenerator.create_generation_batch(
        "ch-1", [_unit(*rows[0]), _unit(*rows[1])], request_id="request-retry"
    )
    first = AsyncMock(side_effect=[_response("kp-1", "chunk-1", rows[0][2]), {"cards": []}])
    monkeypatch.setattr(LLMClient, "chat_json", first)
    await RecallGenerator.execute_generation_task(batch["task_id"], batch["batch_id"])
    # A new task can retry the same immutable batch.  kp-1 is restored from its
    # checkpoint and only kp-2 calls the provider.
    retry_task = TaskWorker.create_task("generate_recall_cards", batch["batch_id"])
    second = AsyncMock(return_value=_response("kp-2", "chunk-2", rows[1][2]))
    monkeypatch.setattr(LLMClient, "chat_json", second)
    result = await RecallGenerator.execute_generation_task(retry_task, batch["batch_id"])
    assert result["status"] == "completed"
    second.assert_awaited_once()
    with db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM recall_cards").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_cancellation_does_not_write_current_unit(monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    _, rows = _seed(kp_ids=("kp-1", "kp-2"))
    batch = RecallGenerator.create_generation_batch(
        "ch-1", [_unit(*rows[0]), _unit(*rows[1])], request_id="request-cancel"
    )

    async def cancel_after_first(*args, **kwargs):
        TaskWorker.request_cancel(batch["task_id"])
        return _response("kp-1", "chunk-1", rows[0][2])

    monkeypatch.setattr(LLMClient, "chat_json", cancel_after_first)
    result = await RecallGenerator.execute_generation_task(batch["task_id"], batch["batch_id"])
    assert result["status"] == "cancelled"
    with db_session() as conn:
        # Cancellation requested before persistence means current unit is not
        # inserted, and no later unit can start.
        assert conn.execute("SELECT COUNT(*) FROM recall_cards").fetchone()[0] == 0
        assert conn.execute(
            "SELECT status FROM recall_generation_batches WHERE id = ?", (batch["batch_id"],)
        ).fetchone()[0] == "cancelled"


@pytest.mark.asyncio
async def test_formal_mode_rejects_demo_fallback(monkeypatch):
    _, rows = _seed()
    batch = RecallGenerator.create_generation_batch(
        "ch-1", [_unit(*rows[0])], request_id="request-formal", mode="formal"
    )
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    result = await RecallGenerator.execute_generation_task(batch["task_id"], batch["batch_id"])
    assert result["status"] == "failed"
    with db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM recall_cards").fetchone()[0] == 0
