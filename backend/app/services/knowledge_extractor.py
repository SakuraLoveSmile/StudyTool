import asyncio
import json
import logging
import re
import time
import uuid
import hashlib
from typing import List, Dict, Any, Optional
from app.database import db_session
from app.models.schemas import (
    CandidateDecisionItem, QuestionLinkDecision,
    ExtractionBatchResponse, CandidateResponse, CandidateCitation,
    ApplyCandidatesResponse, QuestionLinkSuggestionResponse, QuestionLinkItem,
    QuestionLinkApplyResponse, CandidateSourceReviewRequest, CandidateSourceReview
)
from app.services.llm.client import LLMClient
from app.services.llm.prompts import KNOWLEDGE_POINT_EXTRACT_SYSTEM_PROMPT
from app.services.task_worker import TaskWorker
from app.diagnostics import unit_context, log_diagnostic

logger = logging.getLogger(__name__)

class KnowledgeExtractor:
    @classmethod
    def create_extraction_batch(cls, doc_ids: List[str]) -> Dict[str, Any]:
        """
        Creates an extraction batch and enqueues a background extraction task.
        """
        if not doc_ids:
            raise ValueError("请至少选择一份资料")

        with db_session() as conn:
            # 1. Verify documents exist
            placeholders = ",".join(["?"] * len(doc_ids))
            docs = conn.execute(
                f"SELECT id, filename, status FROM documents WHERE id IN ({placeholders})",
                doc_ids
            ).fetchall()
            if len(docs) != len(doc_ids):
                raise ValueError("部分所选资料不存在")

            # 2. Verify documents have parsed text chunks
            chunk_count = conn.execute(
                f"""SELECT count(*) as c FROM document_chunks 
                    WHERE doc_id IN ({placeholders}) 
                      AND (parse_error IS NULL OR parse_error = '') 
                      AND TRIM(content) != ''""",
                doc_ids
            ).fetchone()["c"]
            if chunk_count == 0:
                raise ValueError("所选资料未发现有效文本切片，请确认资料已成功解析并包含文本内容")

            batch_id = f"ext-{uuid.uuid4().hex[:10]}"
            task_id = TaskWorker.create_task(
                "extract_knowledge_points",
                target_id=batch_id,
                message="考点提取任务已进入队列"
            )

            conn.execute("""
                INSERT INTO extraction_batches (id, task_id, doc_ids_json, status)
                VALUES (?, ?, ?, 'processing')
            """, (batch_id, task_id, json.dumps(doc_ids)))

            row = conn.execute("SELECT * FROM extraction_batches WHERE id = ?", (batch_id,)).fetchone()
            return cls._batch_row_to_dict(conn, row)

    @classmethod
    def _batch_row_to_dict(cls, conn, row) -> Dict[str, Any]:
        cand_count = conn.execute(
            "SELECT count(*) as c FROM extraction_candidates WHERE extraction_id = ?",
            (row["id"],)
        ).fetchone()["c"]
        pending_count = conn.execute(
            "SELECT count(*) as c FROM extraction_candidates WHERE extraction_id = ? AND status = 'pending'",
            (row["id"],)
        ).fetchone()["c"]
        task_status = None
        if row["task_id"]:
            t = conn.execute("SELECT status FROM tasks WHERE id = ?", (row["task_id"],)).fetchone()
            task_status = t["status"] if t else None
        return {
            "id": row["id"],
            "task_id": row["task_id"],
            "status": row["status"],
            "doc_ids": json.loads(row["doc_ids_json"]) if row["doc_ids_json"] else [],
            "coverage_info": json.loads(row["coverage_info_json"]) if row["coverage_info_json"] else None,
            "error_msg": row["error_msg"],
            "candidates_count": cand_count,
            "pending_candidates": pending_count,
            "task_status": task_status,
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"])
        }

    @classmethod
    def get_extraction_batch(cls, extraction_id: str) -> Optional[Dict[str, Any]]:
        with db_session() as conn:
            row = conn.execute("SELECT * FROM extraction_batches WHERE id = ?", (extraction_id,)).fetchone()
            if not row:
                return None
            return cls._batch_row_to_dict(conn, row)

    @classmethod
    def get_latest_extraction(cls) -> Optional[Dict[str, Any]]:
        """Returns the most recent extraction batch so the UI can resume a pending review."""
        with db_session() as conn:
            row = conn.execute("""
                SELECT * FROM extraction_batches
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
            """).fetchone()
            if not row:
                return None
            return cls._batch_row_to_dict(conn, row)

    @classmethod
    def list_extractions(cls, *, pending_only: bool = False, limit: int = 20, offset: int = 0) -> Dict[str, Any]:
        """List persisted batches without creating or touching a worker task."""
        where = ("WHERE b.status IN ('completed', 'partial') AND EXISTS "
                 "(SELECT 1 FROM extraction_candidates c WHERE c.extraction_id = b.id AND c.status = 'pending')") if pending_only else ""
        with db_session() as conn:
            total = conn.execute(f"SELECT count(*) AS c FROM extraction_batches b {where}").fetchone()["c"]
            rows = conn.execute(
                f"SELECT b.* FROM extraction_batches b {where} ORDER BY b.created_at DESC, b.id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return {"items": [cls._batch_row_to_dict(conn, row) for row in rows], "total": total}

    @staticmethod
    def _source_hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @classmethod
    def _candidate_dict(cls, conn, row) -> Dict[str, Any]:
        data = dict(row)
        raw = json.loads(data.get("citations_json") or "[]")
        citations = [CandidateCitation(**citation) for citation in raw]
        return {
            "id": data["id"], "extraction_id": data["extraction_id"], "name": data["name"],
            "chapter": data.get("chapter"), "definition": data.get("definition"),
            "citations": citations, "status": data.get("status", "pending"),
            "match_type": data.get("match_type", "new"), "matched_kp_id": data.get("matched_kp_id"),
            "matched_kp_name": data.get("matched_kp_name"), "target_kp_id": data.get("target_kp_id"),
            "source_status": data.get("source_status") or "valid",
            "requires_source_review": (data.get("source_status") or "valid") != "valid",
            "version": int(data.get("version") or 1), "created_at": str(data.get("created_at")),
        }

    @classmethod
    def get_candidate_source_review(cls, extraction_id: str, candidate_id: str) -> Optional[Dict[str, Any]]:
        with db_session() as conn:
            row = conn.execute(
                "SELECT c.*, kp.name AS matched_kp_name FROM extraction_candidates c "
                "LEFT JOIN knowledge_points kp ON c.matched_kp_id = kp.id "
                "WHERE c.id = ? AND c.extraction_id = ?", (candidate_id, extraction_id)
            ).fetchone()
            if not row:
                return None
            data = dict(row)
            citations = []
            can_confirm = data.get("status") == "pending"
            reasons = []
            batch = conn.execute("SELECT status FROM extraction_batches WHERE id = ?", (extraction_id,)).fetchone()
            if not batch or batch["status"] not in ("completed", "partial"):
                can_confirm = False
                reasons.append("提取批次尚未完成")
            if data.get("status") == "invalid":
                reasons.append("历史来源失效候选的原审核决定无法确认，请人工核对")
            stored_citations = json.loads(data.get("citations_json") or "[]")
            if not stored_citations:
                can_confirm = False
                reasons.append("该候选没有可核对的原文引用")
            for citation in stored_citations:
                chunk = conn.execute(
                    "SELECT c.id, c.doc_id, c.content, c.page_number, c.paragraph_ref, d.filename "
                    "FROM document_chunks c JOIN documents d ON d.id = c.doc_id WHERE c.id = ?",
                    (citation.get("chunk_id"),)
                ).fetchone()
                current = dict(citation)
                if not chunk:
                    can_confirm = False
                    reasons.append("引用的资料切片已不存在")
                    current.update({"content": "", "source_hash": None})
                    citations.append(current)
                    continue
                content = chunk["content"] or ""
                current_hash = cls._source_hash(content)
                current.update({"content": content, "source_hash": current_hash,
                                "doc_name": chunk["filename"], "page_number": chunk["page_number"],
                                "paragraph_ref": chunk["paragraph_ref"]})
                if citation.get("doc_id") != chunk["doc_id"] or not content.strip():
                    can_confirm = False
                    reasons.append("引用资料与当前原文不一致")
                elif not citation.get("quote") or citation["quote"] not in content:
                    current["quote"] = content.strip()[:120]
                    reasons.append("原引用已变化，请逐项核对当前原文")
                citations.append(current)
            if data.get("source_status") not in (None, "valid"):
                reasons.append("来源已失效，请重新核对")
            return {
                "candidate_id": candidate_id, "version": int(data.get("version") or 1),
                "name": data["name"], "chapter": data.get("chapter"), "citations": citations,
                "can_confirm": can_confirm, "reason": "; ".join(dict.fromkeys(reasons)) or None,
            }

    @classmethod
    def confirm_candidate_source_review(cls, extraction_id: str, candidate_id: str, req: CandidateSourceReviewRequest) -> Dict[str, Any]:
        with db_session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            batch = conn.execute("SELECT status FROM extraction_batches WHERE id = ?", (extraction_id,)).fetchone()
            if not batch:
                raise ValueError("提取批次不存在")
            if batch["status"] not in ("completed", "partial"):
                raise RuntimeError("提取批次尚未完成，暂不能核对来源")
            row = conn.execute(
                "SELECT * FROM extraction_candidates WHERE id = ? AND extraction_id = ?", (candidate_id, extraction_id)
            ).fetchone()
            if not row:
                raise ValueError("候选考点不存在")
            if int(row["version"] or 1) != req.version:
                raise RuntimeError("候选版本已变化，请重新加载来源")
            if row["status"] != "pending":
                raise RuntimeError("该候选已审核，不能重复核对")
            stored_citations = json.loads(row["citations_json"] or "[]")
            if len(req.citations) != len(stored_citations) or [c.chunk_id for c in req.citations] != [c.get("chunk_id") for c in stored_citations]:
                raise RuntimeError("引用列表已变化，请重新加载后核对")
            normalized = []
            for citation in req.citations:
                chunk = conn.execute(
                    "SELECT c.id, c.doc_id, c.content, c.page_number, c.paragraph_ref, d.filename "
                    "FROM document_chunks c JOIN documents d ON d.id = c.doc_id WHERE c.id = ?",
                    (citation.chunk_id,)
                ).fetchone()
                content = chunk["content"] if chunk else None
                current_hash = cls._source_hash(content) if content is not None else None
                if (not chunk or chunk["doc_id"] != citation.doc_id or not content.strip()
                    or citation.source_hash != current_hash or not citation.quote or citation.quote not in content):
                    raise RuntimeError("来源已变化或引用不可用，请重新加载后核对")
                normalized.append({
                    "chunk_id": chunk["id"], "doc_id": chunk["doc_id"],
                    "doc_name": chunk["filename"], "page_number": chunk["page_number"],
                    "paragraph_ref": chunk["paragraph_ref"], "quote": citation.quote,
                    "source_hash": current_hash,
                })
            update_cursor = conn.execute(
                "UPDATE extraction_candidates SET name = ?, chapter = ?, citations_json = ?, source_status = 'valid', "
                "match_type = CASE WHEN match_type = 'source_changed' AND matched_kp_id IS NULL THEN 'new' "
                "WHEN match_type = 'source_changed' THEN 'existing' ELSE match_type END, version = version + 1 "
                "WHERE id = ? AND extraction_id = ? AND version = ? AND status = 'pending'",
                (req.name.strip(), req.chapter.strip() if req.chapter else None, json.dumps(normalized, ensure_ascii=False), candidate_id, extraction_id, req.version),
            )
            if update_cursor.rowcount != 1:
                raise RuntimeError("候选版本已变化，请重新加载来源")
            updated = conn.execute(
                "SELECT c.*, kp.name AS matched_kp_name FROM extraction_candidates c LEFT JOIN knowledge_points kp ON c.matched_kp_id = kp.id WHERE c.id = ?", (candidate_id,)
            ).fetchone()
            return cls._candidate_dict(conn, updated)

    @classmethod
    async def execute_extraction_task(cls, task_id: str, batch_id: str):
        """
        Background worker execution: reads chunks, calls LLM with bounded concurrency (2),
        saves candidates incrementally per sub-batch so users can preview results in real time,
        supports checkpoint resumption, records granular events and timings, and truthfully handles partial failures.
        """
        TaskWorker.log_event(task_id, "stage_change", "正在读取资料切片...", stage="read_chunks")
        TaskWorker.update_progress_detail(
            task_id,
            stage="read_chunks",
            phase="working",
            currentAction="正在读取资料切片...",
            force=True
        )
        TaskWorker.update_task_progress(task_id, 5, "正在读取资料切片...")

        t_db_read = time.monotonic()
        with db_session() as conn:
            batch_row = conn.execute("SELECT * FROM extraction_batches WHERE id = ?", (batch_id,)).fetchone()
        if not batch_row:
            TaskWorker.mark_task_failed(task_id, "提取批次不存在")
            return

        doc_ids = json.loads(batch_row["doc_ids_json"])
        placeholders = ",".join(["?"] * len(doc_ids))

        with db_session() as conn:
            chunks = conn.execute(
                f"""SELECT c.id, c.doc_id, c.page_number, c.paragraph_ref, c.content, c.parse_error, d.filename
                    FROM document_chunks c
                    JOIN documents d ON c.doc_id = d.id
                    WHERE c.doc_id IN ({placeholders})
                    ORDER BY c.doc_id, c.page_number, c.chunk_index""",
                doc_ids
            ).fetchall()
            existing_kps = conn.execute("SELECT id, name, is_deleted FROM knowledge_points").fetchall()

        existing_kps_lookup: Dict[str, Dict[str, Any]] = {
            re.sub(r"\s+", "", kp["name"]).lower(): dict(kp) for kp in existing_kps
        }

        TaskWorker.record_timing(task_id, "db_write", 0.0)

        valid_chunks = [
            c for c in chunks
            if not c["parse_error"] and c["content"] and c["content"].strip()
        ]

        if not valid_chunks:
            TaskWorker.mark_task_failed(task_id, "所选资料中无有效文本切片")
            with db_session() as conn:
                conn.execute(
                    "UPDATE extraction_batches SET status = 'failed', error_msg = '所选资料中无有效文本切片', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (batch_id,)
                )
            return

        # Group chunks into sub-batches (~8 chunks or ~4500 characters per sub-batch)
        sub_batches: List[List[Dict[str, Any]]] = []
        current_sub: List[Dict[str, Any]] = []
        current_len = 0
        for ch in valid_chunks:
            ch_len = len(ch["content"])
            if current_sub and (len(current_sub) >= 8 or current_len + ch_len > 4500):
                sub_batches.append(current_sub)
                current_sub = [dict(ch)]
                current_len = ch_len
            else:
                current_sub.append(dict(ch))
                current_len += ch_len
        if current_sub:
            sub_batches.append(current_sub)

        total_batches = len(sub_batches)
        checkpoints = TaskWorker.get_completed_checkpoints("extract_knowledge_points", batch_id)

        # Count initial candidates if resuming
        with db_session() as conn:
            init_cands_count = conn.execute(
                "SELECT count(*) as c FROM extraction_candidates WHERE extraction_id = ?",
                (batch_id,)
            ).fetchone()["c"]

        succeeded_units = set()
        for b_idx in range(total_batches):
            b_key = f"batch_{b_idx}"
            if b_key in checkpoints:
                succeeded_units.add(b_idx)

        failed_units = set()

        TaskWorker.log_event(
            task_id,
            "stage_change",
            f"开始分批提取考点（共 {total_batches} 批），并发上限为 2",
            stage="batch_extract"
        )
        TaskWorker.update_progress_detail(
            task_id,
            stage="batch_extract",
            phase="working",
            unit="batch",
            total=total_batches,
            succeeded=len(succeeded_units),
            failed=0,
            skipped=0,
            activeUnits=[],
            resultCount=init_cands_count,
            currentScope=None,
            currentAction=f"开始提取考点，共 {total_batches} 批",
            force=True
        )

        sem = asyncio.Semaphore(2)

        async def process_sub_batch(b_idx: int, sub: List[Dict[str, Any]]) -> Dict[str, Any]:
            async with sem:
                if TaskWorker.is_cancelled(task_id):
                    return {"status": "cancelled", "b_idx": b_idx}

                # Compute page range / scope
                pg_numbers = [c["page_number"] for c in sub if c.get("page_number") is not None]
                if pg_numbers:
                    min_pg, max_pg = min(pg_numbers), max(pg_numbers)
                    scope_str = f"第 {min_pg}—{max_pg} 页" if min_pg != max_pg else f"第 {min_pg} 页"
                else:
                    scope_str = sub[0].get("paragraph_ref") or "正文"

                # Independent per-batch unit: batch 5 can never overwrite batch 3's status
                unit_id = f"kbatch-{b_idx + 1}"

                with unit_context(unit_id=unit_id, unit_index=b_idx + 1, scope=scope_str):
                    b_key = f"batch_{b_idx}"
                    if b_key in checkpoints:
                        TaskWorker.log_event(
                            task_id, "checkpoint_restored",
                            f"第 {b_idx + 1} 批命中历史检查点，已复用先前提取结果",
                            stage="batch_extract", unit_index=b_idx + 1
                        )
                        TaskWorker.finish_exec_unit(
                            task_id, unit_id, state="skipped", force=True
                        )
                        log_diagnostic(
                            "checkpoint_restored",
                            level=logging.INFO,
                            message=f"第 {b_idx + 1} 批命中历史检查点，已复用先前提取结果",
                            stage="batch_extract",
                            unit_id=unit_id,
                            unit_index=b_idx + 1,
                            scope=scope_str
                        )
                        return {"status": "restored", "b_idx": b_idx}

                    TaskWorker.upsert_exec_unit(
                        task_id, unit_id,
                        kind="batch", index=b_idx + 1, scope=scope_str, service="llm",
                        state="waiting_slot", action="等待模型调用名额", force=True
                    )
                    log_diagnostic(
                        "unit_started",
                        level=logging.INFO,
                        message=f"第 {b_idx + 1} 批考点提取开始 (范围: {scope_str})",
                        stage="batch_extract",
                        unit_id=unit_id,
                        unit_index=b_idx + 1,
                        scope=scope_str,
                        chunk_count=len(sub)
                    )

                    prompt_parts = []
                    for ch in sub:
                        ref = ch.get("paragraph_ref") or "正文"
                        pg = ch.get("page_number") if ch.get("page_number") is not None else 1
                        prompt_parts.append(
                            f"[切片ID: {ch['id']}] [资料: {ch['filename']}] [页码: P{pg}] [段落: {ref}]\n{ch['content']}\n"
                        )
                    user_prompt = "\n".join(prompt_parts)

                    # Unified lifecycle callback: fixed signature (event, info) that matches
                    # LLMClient.chat_json(on_event=...).
                    on_llm_event = TaskWorker.make_unit_event_handler(
                        task_id, unit_id, label=f"第 {b_idx + 1} 批",
                        stage="batch_extract", unit_index=b_idx + 1, service="llm"
                    )

                    t_req = time.monotonic()
                    try:
                        res = await LLMClient.chat_json(
                            KNOWLEDGE_POINT_EXTRACT_SYSTEM_PROMPT,
                            user_prompt,
                            on_event=on_llm_event
                        )
                    except Exception as exc:
                        elapsed_ms = (time.monotonic() - t_req) * 1000.0
                        TaskWorker.record_timing(task_id, "llm_extract_request", elapsed_ms)
                        failed_units.add(b_idx)
                        TaskWorker.log_event(
                            task_id, "failed",
                            f"第 {b_idx + 1} 批考点提取失败: {TaskWorker.sanitize_error(exc)}",
                            stage="batch_extract", unit_index=b_idx + 1
                        )
                        TaskWorker.finish_exec_unit(
                            task_id, unit_id, state="failed", error=str(exc), force=True
                        )
                        TaskWorker.update_progress_detail(
                            task_id,
                            failed=len(failed_units),
                            force=True
                        )
                        log_diagnostic(
                            "unit_failed",
                            level=logging.ERROR,
                            message=f"第 {b_idx + 1} 批考点提取失败: {TaskWorker.sanitize_error(exc)}",
                            stage="batch_extract",
                            unit_id=unit_id,
                            unit_index=b_idx + 1,
                            scope=scope_str,
                            exception_type=type(exc).__name__
                        )
                        return {"status": "failed", "b_idx": b_idx, "error": str(exc)}

                    elapsed_ms = (time.monotonic() - t_req) * 1000.0
                    TaskWorker.record_timing(task_id, "llm_extract_request", elapsed_ms)

                    sub_chunk_map = {c["id"]: c for c in sub}
                    raw_candidates = res.get("candidates", []) if isinstance(res, dict) else []

                    batch_cands = []
                    for item in raw_candidates:
                        if not isinstance(item, dict):
                            continue
                        name = str(item.get("name", "")).strip()
                        if not name or len(name) < 2:
                            continue

                        chapter = str(item.get("chapter", "")).strip() or None
                        definition = str(item.get("definition", "")).strip() or None
                        raw_src_ids = item.get("source_chunk_ids", [])
                        if not isinstance(raw_src_ids, list):
                            raw_src_ids = []

                        valid_src_ids = [sid for sid in raw_src_ids if sid in sub_chunk_map]
                        if not valid_src_ids:
                            valid_src_ids = [sub[0]["id"]]

                        citations = []
                        raw_quote = str(item.get("quote", "")).strip()

                        for sid in valid_src_ids:
                            ch_info = sub_chunk_map[sid]
                            if raw_quote and raw_quote in ch_info["content"]:
                                quote = raw_quote
                            else:
                                quote = ch_info["content"].strip()[:120]

                            citations.append({
                                "chunk_id": sid,
                                "doc_id": ch_info["doc_id"],
                                "doc_name": ch_info["filename"],
                                "page_number": ch_info["page_number"],
                                "paragraph_ref": ch_info["paragraph_ref"],
                                "quote": quote,
                                "source_hash": cls._source_hash(ch_info["content"] or ""),
                            })

                        batch_cands.append({
                            "name": name,
                            "chapter": chapter,
                            "definition": definition,
                            "citations": citations
                        })

                    # Transactionally save candidates for this sub-batch into SQLite
                    t_save = time.monotonic()
                    with db_session() as conn:
                        existing_cands_rows = conn.execute(
                            "SELECT id, name, citations_json, definition, chapter FROM extraction_candidates WHERE extraction_id = ?",
                            (batch_id,)
                        ).fetchall()
                        existing_cands_map = {
                            re.sub(r"\s+", "", r["name"]).lower(): dict(r) for r in existing_cands_rows
                        }

                        for cand in batch_cands:
                            cand_norm = re.sub(r"\s+", "", cand["name"]).lower()
                            if cand_norm in existing_cands_map:
                                ex = existing_cands_map[cand_norm]
                                ex_cits = json.loads(ex["citations_json"]) if ex.get("citations_json") else []
                                ex_cids = {c["chunk_id"] for c in ex_cits}
                                for c in cand["citations"]:
                                    if c["chunk_id"] not in ex_cids:
                                        ex_cits.append(c)
                                        ex_cids.add(c["chunk_id"])
                                conn.execute("""
                                    UPDATE extraction_candidates
                                    SET citations_json = ?,
                                        definition = COALESCE(definition, ?),
                                        chapter = COALESCE(chapter, ?)
                                    WHERE id = ?
                                """, (
                                    json.dumps(ex_cits, ensure_ascii=False),
                                    cand["definition"],
                                    cand["chapter"],
                                    ex["id"]
                                ))
                            else:
                                matched = existing_kps_lookup.get(cand_norm)
                                if matched:
                                    match_type = "deleted_or_ignored" if matched["is_deleted"] == 1 else "existing"
                                    matched_kp_id = matched["id"]
                                else:
                                    match_type = "new"
                                    matched_kp_id = None

                                cand_id = f"cand-{uuid.uuid4().hex[:10]}"
                                conn.execute("""
                                    INSERT INTO extraction_candidates (
                                        id, extraction_id, name, chapter, definition,
                                        citations_json, status, match_type, matched_kp_id
                                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                                """, (
                                    cand_id, batch_id, cand["name"], cand["chapter"], cand["definition"],
                                    json.dumps(cand["citations"], ensure_ascii=False),
                                    match_type, matched_kp_id
                                ))
                                existing_cands_map[cand_norm] = {
                                    "id": cand_id,
                                    "name": cand["name"],
                                    "citations_json": json.dumps(cand["citations"], ensure_ascii=False),
                                    "definition": cand["definition"],
                                    "chapter": cand["chapter"]
                                }

                        current_cands_count = conn.execute(
                            "SELECT count(*) as c FROM extraction_candidates WHERE extraction_id = ?",
                            (batch_id,)
                        ).fetchone()["c"]

                    db_elapsed = (time.monotonic() - t_save) * 1000.0
                    TaskWorker.record_timing(task_id, "db_write", db_elapsed)

                    TaskWorker.save_checkpoint(
                        "extract_knowledge_points",
                        batch_id,
                        b_key,
                        b_idx,
                        "completed",
                        result_count=len(batch_cands)
                    )

                    succeeded_units.add(b_idx)
                    TaskWorker.log_event(
                        task_id, "save_done",
                        f"第 {b_idx + 1} 批考点候选已入库（识别出 {len(batch_cands)} 个候选）",
                        stage="batch_extract", unit_index=b_idx + 1
                    )
                    TaskWorker.finish_exec_unit(
                        task_id, unit_id, state="succeeded", force=True
                    )
                    TaskWorker.update_progress_detail(
                        task_id,
                        succeeded=len(succeeded_units),
                        resultCount=current_cands_count,
                        phase="working",
                        currentAction=f"第 {b_idx + 1} 批处理完成，累计识别 {current_cands_count} 个考点候选"
                    )
                    pct = min(99, int((len(succeeded_units) / total_batches) * 100))
                    TaskWorker.update_task_progress(
                        task_id, pct,
                        f"正在梳理考点 (已完成 {len(succeeded_units)}/{total_batches} 批)"
                    )
                    log_diagnostic(
                        "unit_completed",
                        level=logging.INFO,
                        message=f"第 {b_idx + 1} 批考点候选已入库（识别出 {len(batch_cands)} 个候选）",
                        stage="batch_extract",
                        unit_id=unit_id,
                        unit_index=b_idx + 1,
                        scope=scope_str,
                        candidate_count=len(batch_cands)
                    )
                    return {"status": "success", "b_idx": b_idx, "cands_count": len(batch_cands)}

        # Run sub-batches concurrently with semaphore=2
        tasks = [process_sub_batch(i, s) for i, s in enumerate(sub_batches)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        if TaskWorker.is_cancelled(task_id):
            TaskWorker.finalize_cancellation(task_id)
            with db_session() as conn:
                conn.execute(
                    "UPDATE extraction_batches SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (batch_id,)
                )
            return

        with db_session() as conn:
            final_cands_count = conn.execute(
                "SELECT count(*) as c FROM extraction_candidates WHERE extraction_id = ?",
                (batch_id,)
            ).fetchone()["c"]

        failed_batch_indices = sorted(list(failed_units))
        succeeded_count = len(succeeded_units)
        processed_chunks_count = sum(len(sub_batches[i]) for i in succeeded_units)

        coverage_info = {
            "total_chunks": len(valid_chunks),
            "processed_chunks": processed_chunks_count,
            "candidates_count": final_cands_count,
            "total_batches": total_batches,
            "succeeded_batches": succeeded_count,
            "failed_batches": [idx + 1 for idx in failed_batch_indices]
        }

        if failed_batch_indices and succeeded_count > 0:
            with db_session() as conn:
                conn.execute("""
                    UPDATE extraction_batches 
                    SET status = 'partial', coverage_info_json = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (json.dumps(coverage_info, ensure_ascii=False), batch_id))
            TaskWorker.mark_task_partial(
                task_id,
                f"考点提取部分完成（成功 {succeeded_count}/{total_batches} 批，识别出 {final_cands_count} 个考点候选，{len(failed_batch_indices)} 批失败）"
            )
        elif failed_batch_indices and succeeded_count == 0:
            with db_session() as conn:
                conn.execute("""
                    UPDATE extraction_batches 
                    SET status = 'failed', error_msg = '所有批次提取均失败', updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (batch_id,))
            TaskWorker.mark_task_failed(task_id, "考点提取所有批次均失败")
        else:
            with db_session() as conn:
                conn.execute("""
                    UPDATE extraction_batches 
                    SET status = 'completed', coverage_info_json = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (json.dumps(coverage_info, ensure_ascii=False), batch_id))
            TaskWorker.mark_task_completed(
                task_id,
                f"考点提取完成，共识别出 {final_cands_count} 个考点候选"
            )

    @classmethod
    def get_candidates(cls, extraction_id: str) -> List[Dict[str, Any]]:
        with db_session() as conn:
            rows = conn.execute("""
                SELECT c.*, kp.name as matched_kp_name
                FROM extraction_candidates c
                LEFT JOIN knowledge_points kp ON c.matched_kp_id = kp.id
                WHERE c.extraction_id = ?
                ORDER BY c.created_at ASC
            """, (extraction_id,)).fetchall()

            return [cls._candidate_dict(conn, row) for row in rows]

    @classmethod
    def apply_candidates(cls, extraction_id: str, decisions: List[CandidateDecisionItem]) -> Dict[str, Any]:
        with db_session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if len({item.candidate_id for item in decisions}) != len(decisions):
                raise RuntimeError("同一候选不能在一次审核中重复提交")
            batch = conn.execute("SELECT * FROM extraction_batches WHERE id = ?", (extraction_id,)).fetchone()
            if not batch:
                raise ValueError("提取批次不存在")
            if batch["status"] not in ("completed", "partial"):
                raise RuntimeError("提取批次尚未完成，暂不能审核")

            plans = []
            replayed = []
            for item in decisions:
                cand = conn.execute(
                    "SELECT * FROM extraction_candidates WHERE id = ? AND extraction_id = ?", (item.candidate_id, extraction_id)
                ).fetchone()
                if not cand:
                    raise ValueError("候选考点不存在或不属于该批次")
                action = item.action
                final_name = item.custom_name.strip() if item.custom_name and item.custom_name.strip() else cand["name"]
                final_chapter = item.custom_chapter.strip() if item.custom_chapter and item.custom_chapter.strip() else cand["chapter"]
                decision_json = json.dumps({"action": action, "target_kp_id": item.target_kp_id, "custom_name": final_name, "custom_chapter": final_chapter}, ensure_ascii=False, sort_keys=True)
                if cand["status"] != "pending":
                    if cand["decision_json"] == decision_json:
                        replayed.append({"candidate_id": item.candidate_id, "status": cand["status"], "target_kp_id": cand["target_kp_id"], "replayed": True})
                        continue
                    raise RuntimeError("候选已被其他审核决定处理，请刷新后重试")
                if int(cand["version"] or 1) != item.version:
                    raise RuntimeError("候选版本已变化，请重新加载后重试")
                if (cand["source_status"] or "valid") != "valid":
                    raise RuntimeError("候选来源已失效，请先完成来源核对")
                citations = json.loads(cand["citations_json"] or "[]")
                if not citations:
                    raise RuntimeError("候选缺少可核对的原文引用")
                for citation in citations:
                    chunk = conn.execute(
                        "SELECT doc_id, content FROM document_chunks WHERE id = ?", (citation.get("chunk_id"),)
                    ).fetchone()
                    if (not chunk or chunk["doc_id"] != citation.get("doc_id")
                        or not citation.get("source_hash")
                        or cls._source_hash(chunk["content"] or "") != citation["source_hash"]
                        or not citation.get("quote") or citation["quote"] not in (chunk["content"] or "")):
                        raise RuntimeError("候选来源已变化，请重新核对后再审核")
                if action == "merge" and not item.target_kp_id:
                    raise ValueError("合并操作必须指定目标考点")
                if action == "merge" and not conn.execute(
                    "SELECT 1 FROM knowledge_points WHERE id = ? AND is_deleted = 0", (item.target_kp_id,)
                ).fetchone():
                    raise ValueError("目标考点不存在或已删除")
                plans.append((item, cand, decision_json, final_name, final_chapter))

            applied_count = ignored_count = merged_count = 0
            results = list(replayed)
            for item, cand, decision_json, final_name, final_chapter in plans:
                action = item.action
                if action == "accept":
                    # Extract citation summary for source_reference
                    cits = json.loads(cand["citations_json"]) if cand["citations_json"] else []
                    ref_snippets = []
                    for c in cits:
                        pg_str = f"P{c.get('page_number')}" if c.get('page_number') is not None else ""
                        ref_snippets.append(f"{c.get('doc_name', '')} {pg_str}".strip())
                    source_ref = "；".join(list(dict.fromkeys(ref_snippets))[:3]) if ref_snippets else "资料提取"

                    target_kp_id = None
                    if cand["matched_kp_id"]:
                        target_kp_id = cand["matched_kp_id"]
                        # If matched KP was soft-deleted, restore it
                        conn.execute("""
                            UPDATE knowledge_points
                            SET is_deleted = 0, deleted_at = NULL, name = ?, chapter = ?,
                                source_type = 'extracted', source_status = 'valid', source_reference = ?
                            WHERE id = ?
                        """, (final_name, final_chapter, source_ref, target_kp_id))
                    else:
                        target_kp_id = f"kp-{uuid.uuid4().hex[:8]}"
                        conn.execute("""
                            INSERT INTO knowledge_points (
                                id, name, chapter, priority_tag,
                                source_type, source_status, source_reference, is_deleted
                            ) VALUES (?, ?, ?, 'normal', 'extracted', 'valid', ?, 0)
                        """, (target_kp_id, final_name, final_chapter, source_ref))

                    conn.execute("""
                        UPDATE extraction_candidates
                        SET status = 'accepted', target_kp_id = ?, decision_json = ?, version = version + 1
                        WHERE id = ? AND extraction_id = ? AND status = 'pending' AND version = ?
                    """, (target_kp_id, decision_json, item.candidate_id, extraction_id, item.version))
                    applied_count += 1
                    results.append({"candidate_id": item.candidate_id, "status": "accepted", "target_kp_id": target_kp_id, "replayed": False})

                elif action == "ignore":
                    conn.execute("""
                        UPDATE extraction_candidates
                        SET status = 'ignored', decision_json = ?, version = version + 1
                        WHERE id = ? AND extraction_id = ? AND status = 'pending' AND version = ?
                    """, (decision_json, item.candidate_id, extraction_id, item.version))
                    ignored_count += 1
                    results.append({"candidate_id": item.candidate_id, "status": "ignored", "target_kp_id": None, "replayed": False})

                elif action == "merge":
                    conn.execute("""
                        UPDATE extraction_candidates
                        SET status = 'merged', target_kp_id = ?, decision_json = ?, version = version + 1
                        WHERE id = ? AND extraction_id = ? AND status = 'pending' AND version = ?
                    """, (item.target_kp_id, decision_json, item.candidate_id, extraction_id, item.version))
                    merged_count += 1
                    results.append({"candidate_id": item.candidate_id, "status": "merged", "target_kp_id": item.target_kp_id, "replayed": False})

        return {
            "applied_count": applied_count,
            "ignored_count": ignored_count,
            "merged_count": merged_count,
            "replayed_count": len(replayed),
            "results": results,
        }

    @classmethod
    def get_question_links(cls, extraction_id: str) -> Dict[str, Any]:
        """
        Suggest question links for extracted/accepted knowledge points.
        Crucial requirement: When confirmed questions count is 0,
        it must return 0 links and an explicit message. Zero hallucinations!
        """
        with db_session() as conn:
            questions = conn.execute("""
                SELECT id, stem, year, session_name, question_num
                FROM questions
                WHERE is_confirmed = 1
            """).fetchall()

            total_confirmed = len(questions)
            if total_confirmed == 0:
                return {
                    "links": [],
                    "total_confirmed_questions": 0,
                    "message": "当前无已确认真题，未生成关联建议。请先在资料库导入并确认真题。"
                }

            # Fetch candidates that were accepted or are in batch
            candidates = conn.execute("""
                SELECT id, name, target_kp_id, matched_kp_id
                FROM extraction_candidates
                WHERE extraction_id = ? AND (status = 'accepted' OR status = 'pending')
            """, (extraction_id,)).fetchall()

            links: List[Dict[str, Any]] = []
            for q in questions:
                stem = q["stem"] or ""
                for cand in candidates:
                    kp_id = cand["target_kp_id"] or cand["matched_kp_id"]
                    if not kp_id:
                        continue

                    cand_name = cand["name"]
                    # Keyword match: exact candidate name or substantial keyword (>= 3 chars)
                    if cand_name in stem:
                        confidence = 1.0
                        reason = f"真题题干包含完整考点名称「{cand_name}」"
                    else:
                        # Extract core token
                        sub_terms = [t for t in re.split(r"[ (（)）/、]", cand_name) if len(t) >= 3]
                        matched_term = next((t for t in sub_terms if t in stem), None)
                        if matched_term:
                            confidence = 0.8
                            reason = f"真题题干包含考点核心词「{matched_term}」"
                        else:
                            continue

                    links.append({
                        "question_id": q["id"],
                        "knowledge_point_id": kp_id,
                        "question_stem": stem[:120],
                        "year": q["year"],
                        "session_name": q["session_name"],
                        "question_num": q["question_num"],
                        "knowledge_point_name": cand_name,
                        "confidence": confidence,
                        "reason": reason
                    })

            return {
                "links": links,
                "total_confirmed_questions": total_confirmed,
                "message": None
            }

    @classmethod
    def apply_question_links(cls, links: List[QuestionLinkDecision]) -> Dict[str, int]:
        applied = 0
        with db_session() as conn:
            for item in links:
                if item.action == "accept":
                    rel_id = str(uuid.uuid4())
                    cursor = conn.cursor()
                    cursor.execute("""
                        INSERT OR IGNORE INTO question_knowledge_rel (id, question_id, knowledge_point_id)
                        VALUES (?, ?, ?)
                    """, (rel_id, item.question_id, item.knowledge_point_id))
                    if cursor.rowcount > 0:
                        applied += 1
        return {"applied_count": applied}
