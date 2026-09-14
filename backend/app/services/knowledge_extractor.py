import asyncio
import json
import logging
import re
import time
import uuid
from typing import List, Dict, Any, Optional
from app.database import db_session
from app.models.schemas import (
    CandidateDecisionItem, QuestionLinkDecision,
    ExtractionBatchResponse, CandidateResponse, CandidateCitation,
    ApplyCandidatesResponse, QuestionLinkSuggestionResponse, QuestionLinkItem,
    QuestionLinkApplyResponse
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
            return {
                "id": row["id"],
                "task_id": row["task_id"],
                "status": row["status"],
                "doc_ids": json.loads(row["doc_ids_json"]),
                "coverage_info": json.loads(row["coverage_info_json"]) if row["coverage_info_json"] else None,
                "error_msg": row["error_msg"],
                "candidates_count": 0,
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"])
            }

    @classmethod
    def get_extraction_batch(cls, extraction_id: str) -> Optional[Dict[str, Any]]:
        with db_session() as conn:
            row = conn.execute("SELECT * FROM extraction_batches WHERE id = ?", (extraction_id,)).fetchone()
            if not row:
                return None
            cand_count = conn.execute(
                "SELECT count(*) as c FROM extraction_candidates WHERE extraction_id = ?",
                (extraction_id,)
            ).fetchone()["c"]
            return {
                "id": row["id"],
                "task_id": row["task_id"],
                "status": row["status"],
                "doc_ids": json.loads(row["doc_ids_json"]) if row["doc_ids_json"] else [],
                "coverage_info": json.loads(row["coverage_info_json"]) if row["coverage_info_json"] else None,
                "error_msg": row["error_msg"],
                "candidates_count": cand_count,
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"])
            }

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
                                content_clean = re.sub(r"\s+", " ", ch_info["content"]).strip()
                                quote = raw_quote if raw_quote else content_clean[:60]

                            citations.append({
                                "chunk_id": sid,
                                "doc_id": ch_info["doc_id"],
                                "doc_name": ch_info["filename"],
                                "page_number": ch_info["page_number"],
                                "paragraph_ref": ch_info["paragraph_ref"],
                                "quote": quote
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

            result = []
            for r in rows:
                c_dict = dict(r)
                citations_raw = json.loads(c_dict["citations_json"]) if c_dict.get("citations_json") else []
                citations = [CandidateCitation(**c) for c in citations_raw]
                result.append({
                    "id": c_dict["id"],
                    "extraction_id": c_dict["extraction_id"],
                    "name": c_dict["name"],
                    "chapter": c_dict["chapter"],
                    "definition": c_dict["definition"],
                    "citations": citations,
                    "status": c_dict["status"],
                    "match_type": c_dict["match_type"],
                    "matched_kp_id": c_dict["matched_kp_id"],
                    "matched_kp_name": c_dict["matched_kp_name"],
                    "target_kp_id": c_dict["target_kp_id"],
                    "created_at": str(c_dict["created_at"])
                })
            return result

    @classmethod
    def apply_candidates(cls, extraction_id: str, decisions: List[CandidateDecisionItem]) -> Dict[str, int]:
        applied_count = 0
        ignored_count = 0
        merged_count = 0

        with db_session() as conn:
            for item in decisions:
                cand = conn.execute(
                    "SELECT * FROM extraction_candidates WHERE id = ? AND extraction_id = ?",
                    (item.candidate_id, extraction_id)
                ).fetchone()
                if not cand:
                    continue

                action = item.action
                if action == "accept":
                    final_name = item.custom_name.strip() if item.custom_name and item.custom_name.strip() else cand["name"]
                    final_chapter = item.custom_chapter.strip() if item.custom_chapter and item.custom_chapter.strip() else cand["chapter"]

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
                            ) VALUES (?, ?, ?, 'medium', 'extracted', 'valid', ?, 0)
                        """, (target_kp_id, final_name, final_chapter, source_ref))

                    conn.execute("""
                        UPDATE extraction_candidates
                        SET status = 'accepted', target_kp_id = ?
                        WHERE id = ?
                    """, (target_kp_id, item.candidate_id))
                    applied_count += 1

                elif action == "ignore":
                    conn.execute("""
                        UPDATE extraction_candidates
                        SET status = 'ignored'
                        WHERE id = ?
                    """, (item.candidate_id,))
                    ignored_count += 1

                elif action == "merge":
                    if item.target_kp_id:
                        conn.execute("""
                            UPDATE extraction_candidates
                            SET status = 'merged', target_kp_id = ?
                            WHERE id = ?
                        """, (item.target_kp_id, item.candidate_id))
                        merged_count += 1

        return {
            "applied_count": applied_count,
            "ignored_count": ignored_count,
            "merged_count": merged_count
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
