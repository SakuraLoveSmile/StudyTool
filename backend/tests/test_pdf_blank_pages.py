"""
STU-3 回归：可证实纯白的 PDF 页不进入 OCR 与失败链路；真实错误仍如实失败。

覆盖矩阵（与计划验收样本一一对应）：
- 文字页 + 中间纯白页 + 文字页：空白页无 OCR 调用、无 chunk、页码保持
- 最后一页纯白：completed 且 page_count 不丢末页
- 无文本纯白图片页：渲染证实纯白后跳过；不是所有图片页都被跳过
- 图形/黑像素/淡字/噪声扫描页：不判空白，按原 OCR/错误语义处理
- 全部纯白：仍按“文档未提取到可用内容”失败
- done + 空文本但页面非纯白：保留“OCR 识别文本为空”诊断错误
- 空白页 + 其他页真实 OCR 失败：仍 failed 且只报真实原因
- 空白页跳过 + embedding 批量失败：仍 failed，error_msg 只含向量化失败
- 失败任务点重试：文档回 pending、新任务入队；重跑后状态/计数正确
- 相同 file_hash 重导 / 删除后重导：既有去重与新文档语义回归

全部使用临时数据库（conftest）与模拟外部服务，不触网。
"""
import asyncio
import hashlib
import shutil
import uuid

import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock

from app.config import settings
from app.database import db_session
from app.main import app
from app.services.parser.ocr_service import OCRService, OCRServiceError, OCRQuotaExhaustedError
from app.services.parser.pdf_parser import PDFParser
from app.services.retrieval.embedding import EmbeddingService
from app.services.task_worker import TaskWorker
from app.routers.materials import retry_task


def _make_pdf(path, page_specs):
    """page_specs: 'text' | 'blank' | 'scan' | 'noise' | 'faint'"""
    doc = fitz.open()
    for spec in page_specs:
        page = doc.new_page(width=300, height=200)
        if spec == "text":
            page.insert_text(
                (20, 40),
                "Chapter one covers the fundamentals of management theory.",
                fontsize=11,
            )
        elif spec == "scan":
            page.draw_rect(fitz.Rect(10, 10, 60, 60), color=(0, 0, 1), fill=(0, 0, 1))
        elif spec == "noise":
            page.draw_rect(fitz.Rect(10, 10, 20, 20), color=(0.4, 0.4, 0.4), fill=(0.4, 0.4, 0.4))
        elif spec == "faint":
            page.draw_rect(fitz.Rect(10, 10, 30, 30), color=(0.96, 0.96, 0.96), fill=(0.96, 0.96, 0.96))
    doc.save(str(path))
    doc.close()
    return path


def _register_document(pdf_path, doc_id=None, file_type="textbook"):
    """把合成 PDF 放进 uploads 并插入 documents 行，返回 doc_id。"""
    doc_id = doc_id or str(uuid.uuid4())
    file_hash = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    target = settings.uploads_path / f"{file_hash}.pdf"
    shutil.copy(pdf_path, target)
    with db_session() as conn:
        conn.execute(
            "INSERT INTO documents (id, filename, file_hash, file_type, status) "
            "VALUES (?, ?, ?, ?, 'pending')",
            (doc_id, pdf_path.name, file_hash, file_type),
        )
    return doc_id


async def _run_parse(doc_id):
    task_id = TaskWorker.create_task("parse_document", doc_id)
    await TaskWorker._execute_parse_document(task_id, doc_id)
    return task_id


def _doc_row(doc_id):
    with db_session() as conn:
        return conn.execute(
            "SELECT status, error_msg, page_count FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()


def _task_row(task_id):
    with db_session() as conn:
        return conn.execute(
            "SELECT status, error_msg FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()


def _chunks_of(doc_id):
    with db_session() as conn:
        return conn.execute(
            "SELECT page_number, content, parse_error FROM document_chunks "
            "WHERE doc_id = ? ORDER BY chunk_index ASC",
            (doc_id,),
        ).fetchall()


@pytest.mark.asyncio
async def test_middle_blank_page_skips_ocr_and_preserves_page_numbers(tmp_path, monkeypatch):
    """文字页 + 中间纯白页 + 文字页：空白页无 OCR 调用、无 chunk、页码保持。"""
    pdf = _make_pdf(tmp_path / "mixed.pdf", ["scan", "blank", "scan"])
    ocr_calls = []
    started_units = []

    async def fake_ocr(img_bytes, fmt="png", on_event=None):
        ocr_calls.append(img_bytes)
        return "识别出的扫描页文字内容"

    monkeypatch.setattr(OCRService, "extract_text_from_image_bytes", fake_ocr)

    progress_msgs = []
    chunks = await PDFParser.parse_pdf(
        pdf,
        progress_callback=lambda p, m: progress_msgs.append(m),
        on_ocr_unit_start=lambda p: started_units.append(p) or None,
    )

    assert len(ocr_calls) == 2, "纯白页不得进入 OCR 队列"
    assert started_units == [1, 3], "OCR 单元只应覆盖真实扫描页"
    assert {c["page_number"] for c in chunks} == {1, 3}
    assert all(c.get("parse_error") is None for c in chunks)
    assert any("空白页" in m for m in progress_msgs), "跳过空白页应有如实进度提示"


@pytest.mark.asyncio
async def test_text_pages_with_blank_middle_need_no_ocr_at_all(tmp_path, monkeypatch):
    """有文字层的混合文档：空白页不补 OCR、不产生 chunk。"""
    pdf = _make_pdf(tmp_path / "text_blank_text.pdf", ["text", "blank", "text"])
    ocr_calls = []

    async def fake_ocr(img_bytes, fmt="png", on_event=None):
        ocr_calls.append(img_bytes)
        return "不应被调用"

    monkeypatch.setattr(OCRService, "extract_text_from_image_bytes", fake_ocr)
    chunks = await PDFParser.parse_pdf(pdf)

    assert ocr_calls == []
    assert {c["page_number"] for c in chunks} == {1, 3}


@pytest.mark.asyncio
async def test_all_blank_pdf_returns_no_chunks(tmp_path, monkeypatch):
    """全部纯白：解析产出为空，交给上层“无可用内容”失败语义。"""
    pdf = _make_pdf(tmp_path / "all_blank.pdf", ["blank", "blank"])
    ocr_calls = []

    async def fake_ocr(img_bytes, fmt="png", on_event=None):
        ocr_calls.append(img_bytes)
        return "不应被调用"

    monkeypatch.setattr(OCRService, "extract_text_from_image_bytes", fake_ocr)
    chunks = await PDFParser.parse_pdf(pdf)
    assert chunks == []
    assert ocr_calls == []


@pytest.mark.asyncio
async def test_nonwhite_scan_noise_and_faint_pages_are_not_skipped(tmp_path, monkeypatch):
    """图形/噪声/淡色页一律不算空白，继续走 OCR。"""
    pdf = _make_pdf(tmp_path / "noisy.pdf", ["scan", "noise", "faint"])
    ocr_calls = []

    async def fake_ocr(img_bytes, fmt="png", on_event=None):
        ocr_calls.append(img_bytes)
        return "识别内容"

    monkeypatch.setattr(OCRService, "extract_text_from_image_bytes", fake_ocr)
    chunks = await PDFParser.parse_pdf(pdf)
    assert len(ocr_calls) == 3
    assert {c["page_number"] for c in chunks} == {1, 2, 3}


@pytest.mark.asyncio
async def test_mixed_document_completes_and_page_count_includes_blank(tmp_path, monkeypatch):
    """任务级：scan + blank + scan → completed，page_count 含空白页。"""
    pdf = _make_pdf(tmp_path / "doc_mixed.pdf", ["scan", "blank", "scan"])
    doc_id = _register_document(pdf)

    async def fake_ocr(img_bytes, fmt="png", on_event=None):
        return "扫描页识别文字"

    monkeypatch.setattr(OCRService, "extract_text_from_image_bytes", fake_ocr)
    task_id = await _run_parse(doc_id)

    doc = _doc_row(doc_id)
    task = _task_row(task_id)
    assert doc["status"] == "completed"
    assert doc["error_msg"] is None
    assert doc["page_count"] == 3, "空白页不得缩减总页数"
    assert task["status"] == "completed"
    chunks = _chunks_of(doc_id)
    assert {c["page_number"] for c in chunks} == {1, 3}
    assert all(c["parse_error"] is None for c in chunks)


@pytest.mark.asyncio
async def test_trailing_blank_page_keeps_page_count(tmp_path, monkeypatch):
    """最后一页纯白：completed 且 page_count 不丢末页。"""
    pdf = _make_pdf(tmp_path / "doc_trailing.pdf", ["text", "text", "blank"])
    doc_id = _register_document(pdf)

    async def fake_ocr(img_bytes, fmt="png", on_event=None):
        return "不应被调用"

    monkeypatch.setattr(OCRService, "extract_text_from_image_bytes", fake_ocr)
    task_id = await _run_parse(doc_id)

    doc = _doc_row(doc_id)
    assert doc["status"] == "completed"
    assert doc["page_count"] == 3
    assert _task_row(task_id)["status"] == "completed"


@pytest.mark.asyncio
async def test_all_blank_document_fails_with_empty_content(tmp_path, monkeypatch):
    """全部纯白：按“文档未提取到可用内容”失败，不伪造完成。"""
    pdf = _make_pdf(tmp_path / "doc_all_blank.pdf", ["blank", "blank"])
    doc_id = _register_document(pdf)

    async def fake_ocr(img_bytes, fmt="png", on_event=None):
        return "不应被调用"

    monkeypatch.setattr(OCRService, "extract_text_from_image_bytes", fake_ocr)
    task_id = await _run_parse(doc_id)

    doc = _doc_row(doc_id)
    task = _task_row(task_id)
    assert doc["status"] == "failed"
    assert "未提取到可用内容" in doc["error_msg"]
    assert task["status"] == "failed"
    assert "未提取到可用内容" in task["error_msg"]


@pytest.mark.asyncio
async def test_empty_ocr_result_on_nonblank_page_keeps_parse_error(tmp_path, monkeypatch):
    """done + 空文本但页面非纯白：保留“OCR 识别文本为空”诊断错误。"""
    pdf = _make_pdf(tmp_path / "doc_nonempty.pdf", ["scan"])
    doc_id = _register_document(pdf)

    async def fake_ocr(img_bytes, fmt="png", on_event=None):
        raise OCRServiceError("百度 OCR 识别文本为空")

    monkeypatch.setattr(OCRService, "extract_text_from_image_bytes", fake_ocr)
    task_id = await _run_parse(doc_id)

    doc = _doc_row(doc_id)
    assert doc["status"] == "failed"
    assert "识别文本为空" in doc["error_msg"]
    chunks = _chunks_of(doc_id)
    assert len(chunks) == 1 and chunks[0]["page_number"] == 1
    assert "识别文本为空" in chunks[0]["parse_error"]


@pytest.mark.asyncio
async def test_blank_page_plus_real_ocr_failure_reports_only_real_reason(tmp_path, monkeypatch):
    """空白页 + 其他页真实 OCR 失败：仍 failed 且只报真实原因。"""
    pdf = _make_pdf(tmp_path / "doc_partial.pdf", ["scan", "blank"])
    doc_id = _register_document(pdf)

    async def fake_ocr(img_bytes, fmt="png", on_event=None):
        raise OCRServiceError("OCR 服务返回空文本")

    monkeypatch.setattr(OCRService, "extract_text_from_image_bytes", fake_ocr)
    task_id = await _run_parse(doc_id)

    doc = _doc_row(doc_id)
    assert doc["status"] == "failed"
    assert "OCR 服务返回空文本" in doc["error_msg"]
    chunks = _chunks_of(doc_id)
    assert len(chunks) == 1, "空白页不得产生错误 chunk"
    assert chunks[0]["page_number"] == 1


@pytest.mark.asyncio
async def test_blank_skip_plus_embedding_failure_reports_only_embedding_reason(tmp_path, monkeypatch):
    """复现反馈场景：空白页跳过 + 向量批量失败 → error_msg 不再混入“OCR 识别文本为空”。"""
    pdf = _make_pdf(tmp_path / "doc_embed_fail.pdf", ["scan", "blank"])
    doc_id = _register_document(pdf)

    async def fake_ocr(img_bytes, fmt="png", on_event=None):
        return "扫描页识别文字"

    monkeypatch.setattr(OCRService, "extract_text_from_image_bytes", fake_ocr)
    monkeypatch.setattr(
        EmbeddingService,
        "get_embeddings_batch",
        AsyncMock(side_effect=RuntimeError("远程向量服务请求失败：ReadTimeout")),
    )
    task_id = await _run_parse(doc_id)

    doc = _doc_row(doc_id)
    task = _task_row(task_id)
    assert doc["status"] == "failed"
    assert task["status"] == "failed"
    assert doc["error_msg"].startswith("向量化失败：")
    assert "切片批量向量化失败" in doc["error_msg"]
    assert "ReadTimeout" in doc["error_msg"]
    assert "识别文本为空" not in doc["error_msg"]
    assert "OCR" not in doc["error_msg"]


@pytest.mark.asyncio
async def test_retry_after_failure_requeues_and_recovers(tmp_path, monkeypatch):
    """失败任务点“重试任务”：文档回 pending、新任务入队；重跑后状态/计数正确。"""
    pdf = _make_pdf(tmp_path / "doc_retry.pdf", ["scan", "blank"])
    doc_id = _register_document(pdf)

    async def failing_ocr(img_bytes, fmt="png", on_event=None):
        raise OCRServiceError("OCR 服务返回空文本")

    monkeypatch.setattr(OCRService, "extract_text_from_image_bytes", failing_ocr)
    task_id = await _run_parse(doc_id)
    assert _doc_row(doc_id)["status"] == "failed"

    new_task = retry_task(task_id)
    assert new_task.id != task_id
    assert new_task.status == "pending"
    doc = _doc_row(doc_id)
    assert doc["status"] == "pending"
    assert doc["error_msg"] is None

    async def ok_ocr(img_bytes, fmt="png", on_event=None):
        return "重试后识别成功的扫描文字"

    monkeypatch.setattr(OCRService, "extract_text_from_image_bytes", ok_ocr)
    await TaskWorker._execute_parse_document(new_task.id, doc_id)

    doc = _doc_row(doc_id)
    assert doc["status"] == "completed"
    assert doc["page_count"] == 2
    assert _task_row(new_task.id)["status"] == "completed"
    chunks = _chunks_of(doc_id)
    assert {c["page_number"] for c in chunks} == {1}
    assert all(c["parse_error"] is None for c in chunks)


def test_same_file_reupload_returns_existing_document(tmp_path):
    """相同 file_hash 重导：返回既有文档且不新建任务（既有语义回归）。"""
    pdf = _make_pdf(tmp_path / "dup.pdf", ["text"])
    client = TestClient(app)
    payload = pdf.read_bytes()

    res1 = client.post(
        "/api/materials/upload",
        files={"file": ("dup.pdf", payload, "application/pdf")},
        data={"file_type": "textbook"},
    )
    assert res1.status_code == 200
    doc1 = res1.json()

    res2 = client.post(
        "/api/materials/upload",
        files={"file": ("dup.pdf", payload, "application/pdf")},
        data={"file_type": "textbook"},
    )
    assert res2.status_code == 200
    doc2 = res2.json()

    assert doc2["id"] == doc1["id"]
    with db_session() as conn:
        task_count = conn.execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE task_type = 'parse_document' AND target_id = ?",
            (doc1["id"],),
        ).fetchone()["c"]
    assert task_count == 1, "重复导入不得新建解析任务"


def test_delete_then_reupload_creates_new_pending_document(tmp_path):
    """删除后重导同一文件：新文档 pending/0、新任务执行，状态如实。"""
    pdf = _make_pdf(tmp_path / "reimp.pdf", ["text"])
    client = TestClient(app)
    payload = pdf.read_bytes()

    res1 = client.post(
        "/api/materials/upload",
        files={"file": ("reimp.pdf", payload, "application/pdf")},
        data={"file_type": "textbook"},
    )
    doc1 = res1.json()

    res_del = client.delete(f"/api/materials/{doc1['id']}")
    assert res_del.status_code == 200

    res2 = client.post(
        "/api/materials/upload",
        files={"file": ("reimp.pdf", payload, "application/pdf")},
        data={"file_type": "textbook"},
    )
    assert res2.status_code == 200
    doc2 = res2.json()

    assert doc2["id"] != doc1["id"]
    assert doc2["status"] == "pending"
    assert doc2["page_count"] == 0
    with db_session() as conn:
        task_count = conn.execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE task_type = 'parse_document' AND target_id = ?",
            (doc2["id"],),
        ).fetchone()["c"]
    assert task_count == 1, "删后重导应创建新的解析任务"


@pytest.mark.asyncio
async def test_progress_never_regresses_when_ocr_completes_out_of_order(tmp_path, monkeypatch):
    """STU-3 P2：scan+blank+scan 且第 3 页先于第 1 页完成 —— 回调百分比非递减、终点 100。"""
    pdf = _make_pdf(tmp_path / "mono.pdf", ["scan", "blank", "scan"])
    ocr_calls = []

    async def fake_ocr(img_bytes, fmt="png", on_event=None):
        ocr_calls.append(img_bytes)
        # 先到的调用（第 1 页）更慢 → 第 3 页先终结，物理页码序 ≠ 完成序
        await asyncio.sleep(0.2 if len(ocr_calls) == 1 else 0.0)
        return f"扫描页识别文字 {len(ocr_calls)}"

    monkeypatch.setattr(OCRService, "extract_text_from_image_bytes", fake_ocr)

    events = []
    chunks = await PDFParser.parse_pdf(
        pdf, progress_callback=lambda p, m: events.append((p, m))
    )

    pcts = [p for p, _ in events]
    assert pcts == sorted(pcts), f"解析进度倒退: {events}"
    assert pcts[-1] == 100
    assert len(ocr_calls) == 2, "空白页不得进入 OCR"
    assert {c["page_number"] for c in chunks} == {1, 3}
    assert all(c.get("parse_error") is None for c in chunks)
    assert not any("正在识别" in m for _, m in events), "不再发送 OCR 开始时回调"
    assert [m for _, m in events if "空白页" in m] == ["第 2 页为空白页，已跳过"]

    # 任务级回归：page_count 仍覆盖空白页，文档如实完成
    doc_id = _register_document(pdf)
    await _run_parse(doc_id)
    doc = _doc_row(doc_id)
    assert doc["status"] == "completed"
    assert doc["page_count"] == 3, "空白页不得缩减总页数"
    assert {c["page_number"] for c in _chunks_of(doc_id)} == {1, 3}


@pytest.mark.asyncio
async def test_quota_exhausted_pages_counted_once_and_progress_converges(tmp_path, monkeypatch):
    """额度熔断：首个 OCR 调用耗尽额度后，排队页走熔断分支也只计一次，进度收敛 70 → 100。"""
    pdf = _make_pdf(tmp_path / "quota.pdf", ["scan", "scan", "scan", "scan"])
    ocr_calls = []

    async def fake_ocr(img_bytes, fmt="png", on_event=None):
        ocr_calls.append(img_bytes)
        # 挂起让第二个并发页也进入真实 OCR 调用，之后才一起熔断
        await asyncio.sleep(0)
        raise OCRQuotaExhaustedError("OCR 月度额度已耗尽")

    monkeypatch.setattr(OCRService, "extract_text_from_image_bytes", fake_ocr)

    events = []
    chunks = await PDFParser.parse_pdf(
        pdf, progress_callback=lambda p, m: events.append((p, m))
    )

    pcts = [p for p, _ in events]
    assert pcts == sorted(pcts), f"解析进度倒退: {events}"
    assert pcts[-1] == 100
    assert pcts.count(70) == 1, "OCR 阶段恰好收敛到 70 一次"
    assert len(ocr_calls) == 2, "熔断后排队页不再真正调用 OCR"
    ocr_phase_msgs = [m for p, m in events if p <= 70]
    assert len(ocr_phase_msgs) == 4, "每个 OCR 页终结恰好上报一次"
    assert all("识别失败" in m for m in ocr_phase_msgs), "熔断页如实上报失败"
    assert len(chunks) == 4 and all(c["parse_error"] for c in chunks)
    assert [m for _, m in events if "等待人工处理" in m] == [
        f"第 {n} 页 OCR 失败，等待人工处理" for n in (1, 2, 3, 4)
    ]
