"""Resolve textbook recall-card requests from canonical, verifiable sources.

Preview performs no writes or provider calls. Generation repeats the same
resolution under a write transaction before creating source-bound directories
and immutable worker batches. Reviewed knowledge points remain a prerequisite.
"""

from __future__ import annotations

import json
import math
from typing import Any

from fastapi import HTTPException

from app.database import db_session
from app.services import library
from app.services.recall_cards import RecallCardsService, normalize_title
from app.services.recall_generator import (
    MAX_CHUNKS_PER_UNIT, MAX_UNIT_CHARS, MAX_UNITS, MAX_REQUEST_ID_CHARS,
    RecallGenerationError, RecallGenerator, _canonical_json, _normalize_id,
    _sha256_text, _text,
)


def _load(conn, doc_id):
    if not doc_id:
        raise RecallGenerationError("请先选择教材")
    try:
        docs, chunks = library._load_scope(conn, [doc_id])
    except HTTPException as exc:
        if exc.status_code == 404:
            raise
        raise RecallGenerationError(str(exc.detail)) from exc
    return docs[doc_id], chunks, library._chapter_groups(doc_id, chunks)


def _scope_hash(members):
    return _sha256_text(_canonical_json([
        {key: c.get(key) for key in ("id", "doc_id", "chunk_index", "heading", "page_number", "paragraph_ref")}
        for c in members
    ]))


def chapter_scope(conn, doc_id, library_chapter_id):
    """Canonical membership used both when enqueuing and before card writes."""
    _, _, groups = _load(conn, doc_id)
    if library_chapter_id not in groups:
        raise RecallGenerationError("章节不存在或不属于所选教材，请刷新后重试")
    title, members = groups[library_chapter_id]
    return title, members, _scope_hash(members)


def _manual_refs(value):
    """Only structured source IDs are evidence; display strings are not IDs."""
    if isinstance(value, dict):
        direct = next((value[k] for k in ("chunk_id", "source_chunk_id", "chunkId") if value.get(k)), None)
        if direct is not None:
            yield {**value, "chunk_id": str(direct)}
        for key, item in value.items():
            if key in {"chunk_ids", "source_chunk_ids", "chunkIds"} and isinstance(item, list):
                for chunk_id in item:
                    if chunk_id:
                        yield {"chunk_id": str(chunk_id)}
            elif key not in {"chunk_id", "source_chunk_id", "chunkId"}:
                yield from _manual_refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from _manual_refs(item)


def _references(conn, kp):
    refs, malformed = [], False
    candidates = conn.execute(
        "SELECT citations_json,source_status FROM extraction_candidates "
        "WHERE target_kp_id=? AND status IN ('accepted','merged') ORDER BY id", (kp["id"],)
    ).fetchall()
    for candidate in candidates:
        try:
            citations = json.loads(candidate["citations_json"])
        except (ValueError, TypeError):
            citations = None
        if not isinstance(citations, list) or not citations:
            malformed = True
            continue
        for citation in citations:
            if not isinstance(citation, dict) or not isinstance(citation.get("chunk_id"), str):
                malformed = True
                continue
            refs.append((citation, True, candidate["source_status"]))
    raw = kp.get("source_reference")
    try:
        manual = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        manual = None
    refs.extend((ref, False, "valid") for ref in _manual_refs(manual))
    return refs, malformed


def _unavailable_members(doc_id, chunks):
    # Retain structural positions only to explain unusable references. These
    # rows never become eligible source text or generation input.
    invalid_ids = {c["id"] for c in chunks
                   if c.get("parse_error") or not str(c.get("content") or "").strip()}
    positions = library._chapter_groups(doc_id, [
        {**c, "content": str(c.get("content") or "").strip() or "（原文不可用）", "parse_error": None}
        for c in chunks
    ])
    return {chapter_id: {c["id"] for c in members if c["id"] in invalid_ids}
            for chapter_id, (_, members) in positions.items()}


def _resolve(conn, kp, doc_id, members, all_chunks, refs, malformed, unavailable_ids=()):
    member_ids = {c["id"] for c in members}
    selected, relevant = {}, []
    for ref, reviewed, status in refs:
        chunk_id = ref["chunk_id"]
        current = all_chunks.get(chunk_id)
        if current is None:
            # A missing citation claiming this document cannot be assigned to a
            # chapter safely. Other books' citations do not poison a merged KP.
            if ref.get("doc_id") == doc_id:
                raise RecallGenerationError("引用的教材切片已缺失，请核对来源")
            if not ref.get("doc_id") and not conn.execute(
                "SELECT 1 FROM document_chunks WHERE id=?", (chunk_id,)
            ).fetchone():
                raise RecallGenerationError("引用的原文切片已缺失，无法确定教材归属，请核对来源")
            continue
        if chunk_id in unavailable_ids:
            raise RecallGenerationError("引用的原文不可用，请重新解析并核对来源")
        if chunk_id not in member_ids:
            continue
        if kp.get("source_status") not in (None, "valid") or status not in (None, "valid"):
            raise RecallGenerationError("知识点或已审核引用来源已失效，请先核对来源")
        content = str(current.get("content") or "")
        if current.get("parse_error") or not content.strip():
            raise RecallGenerationError("引用的原文不可用，请重新解析并核对来源")
        if reviewed or "doc_id" in ref:
            if ref.get("doc_id") != current["doc_id"]:
                raise RecallGenerationError("引用资料归属不一致，请核对来源")
        if reviewed or "source_hash" in ref:
            if ref.get("source_hash") != _sha256_text(content):
                raise RecallGenerationError("引用原文已变化，请重新核对来源")
        if reviewed or "quote" in ref:
            quote = ref.get("quote")
            if not isinstance(quote, str) or not quote.strip() or quote not in content:
                raise RecallGenerationError("引用摘录与原文不一致，请重新核对来源")
        selected[chunk_id] = current
        relevant.append(ref)
    if selected:
        if malformed:
            raise RecallGenerationError("已审核引用格式不完整，请先核对来源")
        result = [c for c in members if c["id"] in selected]
        if len(result) > MAX_CHUNKS_PER_UNIT:
            raise RecallGenerationError(f"原文超过 {MAX_CHUNKS_PER_UNIT} 个切片，请先细分知识点")
        if sum(len(c["content"]) for c in result) > MAX_UNIT_CHARS:
            raise RecallGenerationError(f"原文超过 {MAX_UNIT_CHARS} 字符，请先细分知识点")
        return result, relevant
    return [], []


def validated_chunks(conn, kp, doc_id, library_chapter_id):
    """Re-resolve evidence; client-supplied chunk lists are never trusted."""
    _, chunks, groups = _load(conn, doc_id)
    if library_chapter_id not in groups:
        raise RecallGenerationError("所选教材章节已变化，请刷新后重试")
    refs, malformed = _references(conn, kp)
    resolved, _ = _resolve(conn, kp, doc_id, groups[library_chapter_id][1],
                           {c["id"]: c for c in chunks}, refs, malformed,
                           _unavailable_members(doc_id, chunks).get(library_chapter_id, set()))
    if not resolved:
        raise RecallGenerationError("知识点没有属于所选教材章节的可靠原文引用")
    return resolved


def _preview(conn, doc_id, library_chapter_id=None):
    doc, chunks, groups = _load(conn, doc_id)
    if library_chapter_id is not None:
        if library_chapter_id not in groups:
            raise RecallGenerationError("章节不存在或不属于所选教材，请刷新后重试")
        groups = {library_chapter_id: groups[library_chapter_id]}
    all_chunks = {c["id"]: c for c in chunks}
    unavailable = _unavailable_members(doc_id, chunks)
    kps = [dict(r) for r in conn.execute("SELECT * FROM knowledge_points WHERE is_deleted=0 ORDER BY id")]
    references = {kp["id"]: _references(conn, kp) for kp in kps}
    plan, skipped, outcomes, chapter_items = [], [], [], []
    skipped_ids = set()
    for chapter_id, (title, members) in groups.items():
        chapter_plan = []
        directory = conn.execute(
            "SELECT id,status FROM recall_chapters WHERE source_doc_id=? AND normalized_title=?",
            (doc_id, normalize_title(title)),
        ).fetchone()
        for kp in kps:
            refs, malformed = references[kp["id"]]
            try:
                resolved, relevant = _resolve(conn, kp, doc_id, members, all_chunks, refs, malformed,
                                              unavailable.get(chapter_id, set()))
                if not resolved:
                    continue
                if directory and directory["status"] not in (None, "active"):
                    raise RecallGenerationError(f"教材章节“{title}”的回忆卡目录已归档，请先恢复目录")
                item = {"id": kp["id"], "name": kp["name"],
                        "library_chapter_id": chapter_id, "chapter_title": title}
                chapter_plan.append(item)
                outcomes.append({**item, "source_status": kp.get("source_status"), "references": relevant})
            except RecallGenerationError as exc:
                # One source can be valid for another chapter; report this scope
                # explicitly while still allowing its independently valid unit.
                key = (kp["id"], str(exc))
                if key not in skipped_ids:
                    skipped.append({"knowledge_point_id": kp["id"], "name": kp["name"], "reason": str(exc)})
                    skipped_ids.add(key)
        plan.extend(chapter_plan)
        chapter_items.append({"library_chapter_id": chapter_id, "title": title,
                              "knowledge_point_count": len(chapter_plan),
                              "batch_count": math.ceil(len(chapter_plan) / MAX_UNITS)})
    # Unlocatable legacy points are visible as unavailable; never assign them
    # from a chapter-name guess. Proven foreign-book references remain absent.
    for kp in kps:
        refs, malformed = references[kp["id"]]
        # Whole-book preview also reports references in a chapter whose entire
        # usable text disappeared, so canonical eligible chapters omit it.
        if library_chapter_id is None and any(
            ref["chunk_id"] in all_chunks and (
                all_chunks[ref["chunk_id"]].get("parse_error") or
                not str(all_chunks[ref["chunk_id"]].get("content") or "").strip()
            ) for ref, _, _ in refs
        ):
            reason = "引用的原文不可用，请重新解析并核对来源"
            if (kp["id"], reason) not in skipped_ids:
                skipped.append({"knowledge_point_id": kp["id"], "name": kp["name"], "reason": reason})
                skipped_ids.add((kp["id"], reason))
        if not refs:
            skipped.append({"knowledge_point_id": kp["id"], "name": kp["name"],
                            "reason": "缺少可靠原文引用，暂不能确定教材归属，请先提取并审核知识点"})
    fingerprint = _sha256_text(_canonical_json({
        "doc": {k: doc.get(k) for k in ("id", "filename", "file_hash", "status")},
        "library_chapter_id": library_chapter_id,
        "source": {key: [{field: c.get(field) for field in
                    ("id", "doc_id", "chunk_index", "content", "heading", "page_number", "paragraph_ref", "parse_error")}
                   for c in members] for key, (_, members) in groups.items()},
        "outcomes": outcomes, "skipped": skipped, "chapters": chapter_items,
    }))
    return {"doc_id": doc_id, "doc_name": doc["filename"], "library_chapter_id": library_chapter_id,
            "fingerprint": fingerprint, "eligible_count": len(plan), "skipped_count": len(skipped),
            "batch_count": sum(c["batch_count"] for c in chapter_items),
            "chapters": chapter_items, "skipped": skipped, "knowledge_points": plan}


def preview(doc_id: str, library_chapter_id: str | None = None) -> dict[str, Any]:
    with db_session() as conn:
        conn.execute("BEGIN")
        return _preview(conn, doc_id, library_chapter_id)


def generate(doc_id: str, library_chapter_id: str | None = None, *, instructions: str = "",
             request_id: str, preview_fingerprint: str) -> dict[str, Any]:
    request_id = _normalize_id(request_id, field="request_id")
    if len(request_id) > MAX_REQUEST_ID_CHARS:
        raise RecallGenerationError(f"request_id超过 {MAX_REQUEST_ID_CHARS} 字符限制")
    preview_fingerprint = _text(preview_fingerprint, field="预览指纹", required=True)
    instructions = _text(instructions, field="生成要求", max_chars=4000)
    with db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = _preview(conn, doc_id, library_chapter_id)
        if current["fingerprint"] != preview_fingerprint:
            raise RecallGenerationError("教材、知识点或来源已变化，请刷新预览后重新生成")
        request_fp = _sha256_text(_canonical_json({"doc_id": doc_id,
            "library_chapter_id": library_chapter_id, "instructions": instructions,
            "preview_fingerprint": preview_fingerprint}))
        for row in conn.execute("SELECT * FROM recall_generation_batches ORDER BY created_at,id"):
            meta = RecallGenerator._decode_json(row["metadata_json"], {})
            prior = meta.get("scope_request", {})
            if prior.get("request_id") != request_id:
                continue
            if prior.get("fingerprint") != request_fp:
                raise RecallGenerationError("request_id已用于不同的教材生成请求")
            generations = []
            for batch_id in prior["batch_ids"]:
                batch = conn.execute("SELECT * FROM recall_generation_batches WHERE id=?", (batch_id,)).fetchone()
                if not batch:
                    raise RecallGenerationError("原生成请求的批次已缺失，请使用新请求重试")
                metadata = RecallGenerator._decode_json(batch["metadata_json"], {})
                metadata.pop("scope_request", None)
                task = conn.execute("SELECT id FROM tasks WHERE target_id=? AND task_type=? ORDER BY created_at LIMIT 1",
                                    (batch_id, "generate_recall_cards")).fetchone()
                generations.append(RecallGenerator._batch_response(batch, metadata, task_id=task["id"] if task else None))
            return {"request_id": request_id, "eligible_count": prior["eligible_count"],
                    "batch_count": len(generations), "generations": generations}
        if not current["eligible_count"]:
            raise RecallGenerationError("所选教材范围没有可生成的已审核知识点，请先提取并审核，或核对原文来源")
        generations = []
        for chapter in current["chapters"]:
            ids = [kp["id"] for kp in current["knowledge_points"]
                   if kp["library_chapter_id"] == chapter["library_chapter_id"]]
            if not ids:
                continue
            directory = conn.execute(
                "SELECT id,status FROM recall_chapters WHERE source_doc_id=? AND normalized_title=?",
                (doc_id, normalize_title(chapter["title"])),
            ).fetchone()
            if directory:
                if directory["status"] not in (None, "active"):
                    raise RecallGenerationError("教材对应的回忆卡目录已归档，请先恢复目录")
                chapter_id = directory["id"]
            else:
                chapter_id = RecallCardsService.create_chapter({"title": chapter["title"],
                    "source_doc_id": doc_id, "metadata": {"library_chapter_id": chapter["library_chapter_id"]}}, conn=conn)["id"]
            for offset in range(0, len(ids), MAX_UNITS):
                subrequest = "scope-" + _sha256_text(_canonical_json([request_id, chapter["library_chapter_id"], offset]))
                generations.append(RecallGenerator.create_generation_batch(
                    chapter_id, knowledge_point_ids=ids[offset:offset + MAX_UNITS], request_id=subrequest,
                    source_doc_id=doc_id, library_chapter_id=chapter["library_chapter_id"],
                    instructions=instructions, conn=conn,
                ))
        marker = {"request_id": request_id, "fingerprint": request_fp,
                  "eligible_count": current["eligible_count"], "batch_ids": [g["batch_id"] for g in generations]}
        for generation in generations:
            metadata = {**generation["metadata"], "scope_request": marker}
            conn.execute("UPDATE recall_generation_batches SET metadata_json=?,input_snapshot_json=? WHERE id=?",
                         (json.dumps(metadata, ensure_ascii=False), json.dumps(metadata, ensure_ascii=False), generation["batch_id"]))
        return {"request_id": request_id, "eligible_count": current["eligible_count"],
                "batch_count": len(generations), "generations": generations}
