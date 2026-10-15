"""
任务执行状态可视化优化验证：
- 并发批次逐执行单元状态互不覆盖
- 统一请求生命周期上报（等待名额 / 请求开始 / 响应返回 / 429 超时重试），重试重新计时
- OCR 请求状态与页码定位
- 回调参数契约（修复考点提取重试回调与模型客户端不匹配）
- 部分失败 / 取消 / 终态 / 旧任务兼容 / 错误脱敏
全部使用临时数据库（conftest）与模拟外部服务。
"""
import json
import uuid

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import settings
from app.database import db_session
from app.services.http_client import HttpClientProvider, GlobalRateLimiter
from app.services.llm.client import LLMClient
from app.services.parser.ocr_service import OCRService
from app.services.parser.pdf_parser import PDFParser
from app.services.task_worker import TaskWorker, ACTIVE_UNIT_STATES, utc_now_iso
from app.services.llm.prompts import QUESTION_EXTRACT_SYSTEM_PROMPT



def _make_429():
    resp = MagicMock()
    resp.status_code = 429
    resp.text = "Rate limited"
    resp.headers = {"Retry-After": "0.01"}
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "Rate limited", request=MagicMock(), response=resp
    )
    return resp


def _make_200(content: str = '{"result": "ok"}'):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"choices": [{"message": {"content": content}}]}
    return resp


def _enable_real_llm(monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    monkeypatch.setattr(settings, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(settings, "LLM_BASE_URL", "https://api.example.com/v1")


def test_concurrent_units_do_not_overwrite_each_other():
    """两个并发批次各自持有独立状态，第 5 批不能覆盖第 3 批的等待信息。"""
    task_id = TaskWorker.create_task("extract_knowledge_points", f"ext-{uuid.uuid4().hex[:8]}")

    TaskWorker.upsert_exec_unit(
        task_id, "kbatch-3", kind="batch", index=3, scope="第 9—12 页", service="llm",
        state="awaiting_response", action="等待模型响应", request_started=True, attempt=1, force=True
    )
    TaskWorker.upsert_exec_unit(
        task_id, "kbatch-5", kind="batch", index=5, scope="第 17—20 页", service="llm",
        state="awaiting_response", action="等待模型响应", request_started=True, attempt=1, force=True
    )

    detail = TaskWorker._task_details[task_id]
    units = {u["id"]: u for u in detail["execUnits"]}
    assert units["kbatch-3"]["scope"] == "第 9—12 页"
    assert units["kbatch-5"]["scope"] == "第 17—20 页"
    assert units["kbatch-3"]["requestStartedAt"] and units["kbatch-5"]["requestStartedAt"]
    assert units["kbatch-3"]["requestStartedAt"] != units["kbatch-5"]["requestStartedAt"]

    # 汇总：两个活动批次、无覆盖
    summary = detail["summary"]
    assert summary["activeCount"] == 2
    assert summary["waitingReason"] == "等待模型响应"
    assert summary["waitingSince"] == units["kbatch-3"]["requestStartedAt"]
    assert sorted(detail["activeUnits"]) == [3, 5]

    # 第 5 批完成不影响第 3 批的等待状态
    TaskWorker.finish_exec_unit(task_id, "kbatch-5", state="succeeded", force=True)
    detail = TaskWorker._task_details[task_id]
    units = {u["id"]: u for u in detail["execUnits"]}
    assert units["kbatch-5"]["state"] == "succeeded"
    assert units["kbatch-3"]["state"] == "awaiting_response"
    assert units["kbatch-3"]["requestStartedAt"] is not None
    assert detail["summary"]["activeCount"] == 1
    assert detail["requestStartedAt"] == units["kbatch-3"]["requestStartedAt"]

    # 全部完成后活动请求清空
    TaskWorker.finish_exec_unit(task_id, "kbatch-3", state="succeeded", force=True)
    detail = TaskWorker._task_details[task_id]
    assert detail["activeUnits"] == []
    assert detail["requestStartedAt"] is None
    assert detail["summary"]["activeCount"] == 0


@pytest.mark.asyncio
async def test_llm_request_lifecycle_and_retry_resets_request_start(monkeypatch):
    """统一上报：slot_wait → request_start → retry_wait → request_start(重新计时) → response_received。"""
    _enable_real_llm(monkeypatch)

    responses = [_make_429(), _make_200()]
    post_calls = []

    async def mock_post(*args, **kwargs):
        post_calls.append(kwargs)
        return responses[len(post_calls) - 1]

    client = HttpClientProvider.get_client()
    monkeypatch.setattr(client, "post", mock_post)

    events = []

    async def on_event(event, info):
        events.append((event, dict(info or {})))

    res = await LLMClient.chat_json("sys", "user", max_retries=2, on_event=on_event)
    assert res == {"result": "ok"}
    assert len(post_calls) == 2

    # 第二次尝试前可能再次经过限流退避（slot_wait），因此只断言关键生命周期顺序
    event_names = [e for e, _ in events]
    starts = [info for e, info in events if e == "request_start"]
    retries = [info for e, info in events if e == "retry_wait"]
    responses_ok = [info for e, info in events if e == "response_received"]
    assert [s["attempt"] for s in starts] == [1, 2]  # 请求开始时间每次重试重新记录
    assert len(retries) == 1
    assert retries[0]["attempt"] == 1 and retries[0]["wait_seconds"] > 0 and "429" in retries[0]["reason"]
    assert len(responses_ok) == 1
    assert event_names.index("request_start") < event_names.index("response_received")


@pytest.mark.asyncio
async def test_legacy_on_retry_callback_still_supported(monkeypatch):
    """旧的三参数 on_retry 回调契约保持兼容。"""
    _enable_real_llm(monkeypatch)
    responses = [_make_429(), _make_200()]

    async def mock_post(*args, **kwargs):
        return responses.pop(0)

    client = HttpClientProvider.get_client()
    monkeypatch.setattr(client, "post", mock_post)

    calls = []
    res = await LLMClient.chat_json(
        "sys", "user", max_retries=2,
        on_retry=lambda attempt, delay, reason: calls.append((attempt, delay, reason))
    )
    assert res == {"result": "ok"}
    assert len(calls) == 1 and calls[0][0] == 1


@pytest.mark.asyncio
async def test_ocr_unit_reports_request_state_with_page_scope(monkeypatch):
    """OCR 请求状态上报，且单元携带页码定位。"""
    _enable_real_llm(monkeypatch)
    monkeypatch.setattr(settings, "OCR_MODE", "vision_llm")
    monkeypatch.setattr(settings, "OCR_API_KEY", "test-key")
    monkeypatch.setattr(settings, "OCR_BASE_URL", "https://ocr.example.test/v1")
    # 测试聚焦事件契约，不让 429 全局退避拖慢或污染用例
    monkeypatch.setattr(GlobalRateLimiter, "report_429", AsyncMock())

    task_id = TaskWorker.create_task("parse_document", f"doc-{uuid.uuid4().hex[:8]}")
    handler = TaskWorker.make_unit_event_handler(
        task_id, "page-7", label="第 7 页", stage="parse_file", unit_index=7, service="ocr"
    )
    TaskWorker.upsert_exec_unit(
        task_id, "page-7", kind="page", index=7, scope="第 7 页", service="ocr",
        state="waiting_slot", action="等待OCR调用名额", force=True
    )

    responses = [_make_429(), _make_200(content="识别出的文字")]
    async def mock_post(*args, **kwargs):
        return responses.pop(0)
    client = HttpClientProvider.get_client()
    monkeypatch.setattr(client, "post", mock_post)

    text = await OCRService.extract_text_from_image_bytes(b"img", "png", on_event=handler)
    assert text == "识别出的文字"

    units = {u["id"]: u for u in TaskWorker._task_details[task_id]["execUnits"]}
    unit = units["page-7"]
    # 重试后第二次请求已发起：尝试次数为 2，重试等待随新请求开始被清空
    assert unit["attempt"] == 2
    assert unit["retryReason"] is None
    # 重试等待曾被如实记录（事件流 + 计时）
    with db_session() as conn:
        ev = conn.execute(
            "SELECT message FROM task_events WHERE task_id = ? AND event_type = 'retry_wait' "
            "ORDER BY id DESC LIMIT 1", (task_id,)
        ).fetchone()
    assert ev and "429" in ev["message"]

    TaskWorker.finish_exec_unit(task_id, "page-7", state="succeeded", force=True)
    unit = {u["id"]: u for u in TaskWorker._task_details[task_id]["execUnits"]}["page-7"]
    assert unit["state"] == "succeeded"
    assert unit["finishedAt"] is not None
    assert unit["requestStartedAt"] is None


@pytest.mark.asyncio
async def test_pdf_parser_maps_ocr_units_to_real_page_numbers(tmp_path, monkeypatch):
    """PDF 解析：无文字层的可见内容页触发 OCR，逐页单元携带真实物理页码。"""
    import pymupdf as fitz
    _enable_real_llm(monkeypatch)
    monkeypatch.setattr(settings, "OCR_MODE", "vision_llm")
    monkeypatch.setattr(settings, "OCR_API_KEY", "test-key")
    monkeypatch.setattr(settings, "OCR_BASE_URL", "https://ocr.example.test/v1")

    pdf_path = tmp_path / "scan.pdf"
    doc = fitz.open()
    for _ in range(2):
        p = doc.new_page(width=200, height=200)
        # 画出可见图形：非纯白渲染才需要 OCR（纯白页在 T1 后会被判空跳过）
        p.draw_rect(fitz.Rect(10, 10, 50, 50), color=(0, 0, 1), fill=(0, 0, 1))
    doc.save(pdf_path)
    doc.close()

    task_id = TaskWorker.create_task("parse_document", f"doc-{uuid.uuid4().hex[:8]}")

    def unit_start(page_num):
        unit_id = f"page-{page_num}"
        TaskWorker.upsert_exec_unit(
            task_id, unit_id, kind="page", index=page_num,
            scope=f"第 {page_num} 页", service="ocr",
            state="waiting_slot", action="等待OCR调用名额", force=True
        )
        return TaskWorker.make_unit_event_handler(
            task_id, unit_id, label=f"第 {page_num} 页",
            stage="parse_file", unit_index=page_num, service="ocr"
        )

    def unit_finish(page_num, ok, error):
        TaskWorker.finish_exec_unit(
            task_id, f"page-{page_num}", state="succeeded" if ok else "failed", error=error, force=True
        )

    async def mock_post(*args, **kwargs):
        return _make_200(content=f"页面文字 {uuid.uuid4().hex[:6]}")

    client = HttpClientProvider.get_client()
    monkeypatch.setattr(client, "post", mock_post)

    chunks = await PDFParser.parse_pdf(pdf_path, on_ocr_unit_start=unit_start, on_ocr_unit_finish=unit_finish)

    assert chunks, "应产出切片"
    units = {u["id"]: u for u in TaskWorker._task_details[task_id]["execUnits"]}
    assert set(units.keys()) == {"page-1", "page-2"}
    assert units["page-1"]["scope"] == "第 1 页"
    assert units["page-2"]["scope"] == "第 2 页"
    assert all(u["state"] == "succeeded" for u in units.values())
    # OCR 内容进入了对应页码的切片
    page_numbers = {c["page_number"] for c in chunks}
    assert page_numbers == {1, 2}


def test_retry_wait_state_and_masked_error_on_units():
    task_id = TaskWorker.create_task("extract_questions", f"doc-{uuid.uuid4().hex[:8]}")
    handler = TaskWorker.make_unit_event_handler(
        task_id, "qbatch-1", label="第 1 批", stage="batch_extract", unit_index=1, service="llm"
    )
    import asyncio
    asyncio.run(handler("slot_wait", {"waited_seconds": 1.2}))
    asyncio.run(handler("request_start", {"attempt": 1}))
    asyncio.run(handler("retry_wait", {
        "attempt": 1, "wait_seconds": 3.0,
        "reason": "服务商限流 (429)，等待 3.0 秒后重试"
    }))

    detail = TaskWorker._task_details[task_id]
    unit = detail["execUnits"][0]
    assert unit["state"] == "retry_wait"
    assert unit["attempt"] == 1
    assert "429" in unit["retryReason"]
    assert unit["retryWaitSeconds"] == 3.0
    assert detail["phase"] == "retry_wait"
    assert detail["summary"]["retryingCount"] == 1
    assert detail["summary"]["waitingReason"] == "重试等待"


def test_error_sanitization_masks_secrets():
    raw = "Authorization: Bearer sk-abcdef1234567890 failed\napi_key=supersecret123 with\nnewlines"
    masked = TaskWorker.sanitize_error(raw)
    assert "sk-abcdef1234567890" not in masked
    assert "supersecret123" not in masked
    assert "\n" not in masked
    assert len(masked) <= 200


def test_cancellation_finishes_active_units():
    task_id = TaskWorker.create_task("extract_knowledge_points", f"ext-{uuid.uuid4().hex[:8]}")
    TaskWorker.upsert_exec_unit(
        task_id, "kbatch-2", kind="batch", index=2, scope="第 5—8 页", service="llm",
        state="awaiting_response", action="等待模型响应", request_started=True, force=True
    )
    TaskWorker.finalize_cancellation(task_id)
    unit = TaskWorker._task_details[task_id]["execUnits"][0]
    assert unit["state"] == "cancelled"
    assert unit["requestStartedAt"] is None
    assert TaskWorker._task_details[task_id]["summary"]["activeCount"] == 0


def test_heartbeat_does_not_fake_progress():
    """心跳只更新在线时间，不得伪造实际处理进展。"""
    task_id = TaskWorker.create_task("extract_questions", f"doc-{uuid.uuid4().hex[:8]}")
    TaskWorker.update_progress_detail(task_id, succeeded=3, resultCount=5, force=True)
    before = TaskWorker._task_details[task_id]["lastProgressAt"]
    succeeded_before = TaskWorker._task_details[task_id]["succeeded"]

    # 模拟心跳：只写 heartbeatAt + 强制落盘
    TaskWorker._task_details[task_id]["heartbeatAt"] = utc_now_iso()
    TaskWorker.update_progress_detail(task_id, force=True)

    detail = TaskWorker._task_details[task_id]
    assert detail["lastProgressAt"] == before
    assert detail["succeeded"] == succeeded_before


@pytest.mark.asyncio
async def test_partial_failure_records_failed_unit_and_error(tmp_path):
    """部分批次失败：成功/失败单元分别计数，失败原因脱敏入库。"""
    task_id = TaskWorker.create_task("extract_questions", "doc-partial-units")
    with db_session() as conn:
        conn.execute(
            "INSERT INTO documents (id, filename, file_hash, file_type, status) "
            "VALUES ('doc-partial-units', '试卷.pdf', 'hash-partial-units', 'exam', 'processing')"
        )
        for i in range(9):
            conn.execute(
                "INSERT INTO document_chunks (id, doc_id, chunk_index, page_number, content, content_segmented) "
                "VALUES (?, 'doc-partial-units', ?, ?, ?, ?)",
                (f"chk-pu-{i}", i, i + 1, f"切片内容{i} " + "x" * 40, f"切片内容{i}")
            )

    responses = [
        {"questions": [{"year": 2023, "question_num": "1", "stem": "简述组织形式。"}]},
        RuntimeError("provider timeout"),
    ]
    with patch.object(LLMClient, "chat_json", AsyncMock(side_effect=responses)):
        await TaskWorker._execute_extract_questions(task_id, "doc-partial-units")

    with db_session() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row["status"] == "partial"

    detail = TaskWorker._task_details[task_id]
    units = {u["id"]: u for u in detail["execUnits"]}
    assert units["qbatch-1"]["state"] == "succeeded"
    assert units["qbatch-2"]["state"] == "failed"
    assert "provider timeout" in (units["qbatch-2"]["error"] or "")
    assert detail["succeeded"] == 1 and detail["failed"] == 1
    assert detail["summary"]["failed"] == 1


def test_old_task_progress_detail_without_new_fields_is_accepted():
    """旧任务（缺少 execUnits/summary 字段）接口兼容：显示已有阶段并注明详细调用状态不可用。"""
    old_detail = {
        "stage": "batch_extract", "phase": "working", "unit": "batch",
        "total": 6, "succeeded": 3, "failed": 0, "skipped": 0,
        "activeUnits": [4], "resultCount": 12, "attempt": 1,
        "currentScope": "第 19—24 页", "currentAction": "等待第 4 批模型响应",
        "requestStartedAt": "2026-09-09T08:00:00Z",
        "heartbeatAt": "2026-09-09T08:00:05Z", "lastProgressAt": "2026-09-09T07:59:00Z"
    }
    task_id = str(uuid.uuid4())
    with db_session() as conn:
        conn.execute(
            "INSERT INTO tasks (id, task_type, target_id, status, progress, message, progress_detail_json) "
            "VALUES (?, 'extract_questions', 'doc-old', 'processing', 50, '处理中', ?)",
            (task_id, json.dumps(old_detail, ensure_ascii=False))
        )
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()

    # 与 API 层完全相同的响应构造路径（不额外启动应用 lifespan）
    from app.routers.materials import _task_row_to_response
    resp = _task_row_to_response(row)
    pd = resp.progress_detail
    assert pd is not None
    assert pd.execUnits == []           # 旧任务没有逐执行单元状态
    assert pd.summary is None           # 前端据此提示“详细调用状态不可用”
    assert pd.currentAction == "等待第 4 批模型响应"  # 已有阶段字段保留


def test_new_task_progress_detail_exposes_units_and_summary():
    task_id = TaskWorker.create_task("extract_knowledge_points", f"ext-{uuid.uuid4().hex[:8]}")
    TaskWorker.upsert_exec_unit(
        task_id, "kbatch-1", kind="batch", index=1, scope="第 1—4 页", service="llm",
        state="awaiting_response", action="等待模型响应", request_started=True, force=True
    )
    res_detail = TaskWorker._task_details[task_id]
    assert res_detail["execUnits"] and res_detail["summary"]["activeCount"] == 1

    # API 层能完整解析
    from app.models.schemas import TaskProgressDetail
    parsed = TaskProgressDetail(**res_detail)
    assert parsed.execUnits[0].scope == "第 1—4 页"
    assert parsed.summary.waitingReason == "等待模型响应"
    assert parsed.summary.waitingSince == parsed.execUnits[0].requestStartedAt


@pytest.mark.asyncio
async def test_concurrent_extraction_batches_keep_independent_units():
    """真实并发路径（asyncio.gather + 信号量 2）：两个并发批次的执行单元互不覆盖，
    每批各自持有资料范围与请求开始时间。"""
    import asyncio
    from app.services.llm.client import LLMClient
    from app.services.knowledge_extractor import KnowledgeExtractor

    doc_id = f"doc-conc-{uuid.uuid4().hex[:6]}"
    with db_session() as conn:
        conn.execute(
            "INSERT INTO documents (id, filename, file_hash, file_type, status) "
            "VALUES (?, '并发教材.pdf', 'hash-conc', 'textbook', 'processing')",
            (doc_id,)
        )
        for i in range(20):
            conn.execute(
                "INSERT INTO document_chunks (id, doc_id, chunk_index, page_number, content, content_segmented) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (f"chk-conc-{i}", doc_id, i, i + 1,
                 f"第{i + 1}页的管理职能内容，" + "内容" * 140, f"第{i + 1}页内容")
            )

    batch_dict = KnowledgeExtractor.create_extraction_batch([doc_id])
    task_id = batch_dict["task_id"]
    extraction_id = batch_dict["id"]

    inflight = set()

    async def slow_chat_json(system_prompt, user_prompt, **kwargs):
        # 模拟并发的两个请求同时在途
        inflight.add(user_prompt[:20])
        await asyncio.sleep(0.3)
        inflight.discard(user_prompt[:20])
        return {
            "candidates": [{
                "name": f"考点{''.join(ch for ch in user_prompt[:24] if ch.isalnum())}",
                "chapter": "第一章",
                "definition": "定义",
                "source_chunk_ids": [],
                "quote": "内容"
            }]
        }

    with patch.object(LLMClient, "chat_json", side_effect=slow_chat_json):
        await KnowledgeExtractor.execute_extraction_task(task_id, extraction_id)

    detail = TaskWorker._task_details[task_id]
    units = sorted(detail["execUnits"], key=lambda u: u["index"] or 0)
    assert len(units) >= 2, "应至少产生两个批次单元"
    scopes = [u["scope"] for u in units]
    assert len(set(scopes)) == len(scopes), "各批次资料范围互不覆盖"
    assert all(u["state"] == "succeeded" for u in units)
    assert detail["summary"]["succeeded"] == len(units)
    assert detail["summary"]["activeCount"] == 0


def test_parse_document_explicit_progress_owns_tasks_progress():
    """STU-3 P2：progressMode='explicit' 的任务，detail/单元 flush 不得改写 tasks.progress。"""
    task_id = TaskWorker.create_task("parse_document", f"doc-{uuid.uuid4().hex[:8]}")

    def db_progress():
        with db_session() as conn:
            return conn.execute(
                "SELECT progress FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()["progress"]

    TaskWorker.update_progress_detail(
        task_id, progressMode="explicit", total=5, succeeded=0, force=True
    )
    assert db_progress() == 0, "显式模式 flush 不得派生写 progress"

    for p in (20, 46, 60, 65, 80):
        TaskWorker.update_task_progress(task_id, p, "m")
        assert db_progress() == p
        TaskWorker.update_progress_detail(task_id, force=True)
        assert db_progress() == p, f"detail flush 不得回退显式进度 {p}"
        TaskWorker.upsert_exec_unit(
            task_id, f"page-{p}", kind="page", index=p,
            state="waiting_slot", action="等待OCR调用名额", force=True
        )
        assert db_progress() == p, f"单元 upsert 不得回退显式进度 {p}"
        TaskWorker.finish_exec_unit(task_id, f"page-{p}", state="succeeded", force=True)
        assert db_progress() == p, f"单元 finish 不得回退显式进度 {p}"

    # succeeded/total 打满也不得跳到 99：显式进度由执行器独占
    TaskWorker.update_progress_detail(task_id, succeeded=5, resultCount=5, force=True)
    assert db_progress() == 80

    TaskWorker.mark_task_completed(task_id, "done")
    assert db_progress() == 100


def test_derived_progress_mode_unchanged_for_other_tasks():
    """无 progressMode 标志的任务（extract_questions 等）：flush 仍按 succeeded/total 派生。"""
    task_id = TaskWorker.create_task("extract_questions", f"doc-{uuid.uuid4().hex[:8]}")

    def db_progress():
        with db_session() as conn:
            return conn.execute(
                "SELECT progress FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()["progress"]

    TaskWorker.update_progress_detail(task_id, total=4, succeeded=1, force=True)
    assert db_progress() == 25

    TaskWorker.upsert_exec_unit(
        task_id, "qbatch-1", kind="batch", index=1, scope="第 1—4 页",
        service="llm", state="awaiting_response", action="等待模型响应",
        request_started=True, force=True
    )
    assert db_progress() == 25, "单元 flush 同样走派生 progress"

    TaskWorker.update_task_progress(task_id, 77, "m")
    assert db_progress() == 77

    TaskWorker.update_progress_detail(task_id, succeeded=3, force=True)
    assert db_progress() == 75, "派生模式仍按 succeeded/total 覆盖 progress（既有行为）"

    TaskWorker.finish_exec_unit(task_id, "qbatch-1", state="succeeded", force=True)
    assert db_progress() == 75
