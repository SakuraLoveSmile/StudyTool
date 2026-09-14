import asyncio
import time
import pymupdf as fitz
import logging
from pathlib import Path
from typing import List, Dict, Any, Callable, Optional
from app.services.parser.text_splitter import split_text_into_chunks
from app.services.parser.ocr_service import OCRService, OCRServiceError
from app.diagnostics import unit_context, log_diagnostic

logger = logging.getLogger(__name__)

class PDFParser:
    MAX_PAGE_LIMIT = 500

    @classmethod
    async def parse_pdf(
        cls, 
        file_path: Path, 
        progress_callback: Optional[Callable[[int, str], None]] = None,
        on_ocr_timing: Optional[Callable[[float], None]] = None,
        on_ocr_unit_start: Optional[Callable[[int], Any]] = None,
        on_ocr_unit_finish: Optional[Callable[[int, bool, Optional[str]], None]] = None
    ) -> List[Dict[str, Any]]:
        """
        Parses a PDF file. Preserves exact 1-based page numbers.
        If pages have minimal text and contain images, uses OCRService with bounded concurrency (2 pages).

        on_ocr_unit_start(page_num) -> on_event handler: registers a per-page OCR unit
        and returns its unified lifecycle callback (slot_wait/request_start/response_received/retry_wait).
        on_ocr_unit_finish(page_num, ok, error): marks the page unit terminal.
        """
        doc = fitz.open(file_path)
        total_pages = len(doc)
        
        if total_pages > cls.MAX_PAGE_LIMIT:
            doc.close()
            raise ValueError(f"PDF 页数超过限制 (最大 {cls.MAX_PAGE_LIMIT} 页，当前 {total_pages} 页)")

        # Initial pass: gather text and identify scanned pages needing OCR
        page_data: List[Dict[str, Any]] = []
        ocr_tasks_indices: List[int] = []

        for page_idx in range(total_pages):
            page_num = page_idx + 1
            page = doc[page_idx]
            text = page.get_text("text").strip()
            needs_ocr = len(text) < 25 and (not text or page.get_images(full=True))
            img_bytes = None
            if needs_ocr:
                pix = page.get_pixmap(dpi=150)
                img_bytes = pix.tobytes("png")
                ocr_tasks_indices.append(page_idx)

            page_data.append({
                "page_num": page_num,
                "text": text,
                "needs_ocr": needs_ocr,
                "img_bytes": img_bytes,
                "parse_error": None
            })

        doc.close()

        # Run OCR with bounded concurrency of 2 pages
        if ocr_tasks_indices:
            sem = asyncio.Semaphore(2)
            completed_ocr_count = 0

            async def _run_ocr(p_idx: int):
                nonlocal completed_ocr_count
                entry = page_data[p_idx]
                p_num = entry["page_num"]
                async with sem:
                    unit_id = f"ocr-page-{p_num}"
                    scope_str = f"第 {p_num} 页"
                    with unit_context(unit_id=unit_id, unit_index=p_num, scope=scope_str):
                        log_diagnostic(
                            "unit_started",
                            level=logging.INFO,
                            message=f"第 {p_num} 页 OCR 识别开始",
                            stage="ocr",
                            unit_id=unit_id,
                            unit_index=p_num,
                            scope=scope_str
                        )
                        if progress_callback:
                            progress_callback(
                                int(((p_idx + 1) / total_pages) * 100),
                                f"正在识别第 {p_num} 页图片文字 (OCR 并发处理中)..."
                            )
                        t0 = time.monotonic()
                        ocr_event_handler = None
                        if on_ocr_unit_start:
                            try:
                                ocr_event_handler = on_ocr_unit_start(p_num)
                            except Exception:
                                ocr_event_handler = None
                        try:
                            ocr_res = await OCRService.extract_text_from_image_bytes(
                                entry["img_bytes"], "png", on_event=ocr_event_handler
                            )
                            if ocr_res and ocr_res.strip():
                                entry["text"] = ocr_res
                            else:
                                raise OCRServiceError("OCR 服务返回空文本")
                            if on_ocr_unit_finish:
                                on_ocr_unit_finish(p_num, True, None)
                            log_diagnostic(
                                "unit_completed",
                                level=logging.INFO,
                                message=f"第 {p_num} 页 OCR 识别完成",
                                stage="ocr",
                                unit_id=unit_id,
                                unit_index=p_num,
                                scope=scope_str
                            )
                        except OCRServiceError as exc:
                            entry["parse_error"] = str(exc)
                            entry["text"] = ""
                            if on_ocr_unit_finish:
                                try:
                                    on_ocr_unit_finish(p_num, False, str(exc))
                                except Exception:
                                    pass
                            log_diagnostic(
                                "unit_failed",
                                level=logging.ERROR,
                                message=f"第 {p_num} 页 OCR 识别失败: {str(exc)}",
                                stage="ocr",
                                unit_id=unit_id,
                                unit_index=p_num,
                                scope=scope_str,
                                exception_type=type(exc).__name__
                            )
                        finally:
                            dur_ms = (time.monotonic() - t0) * 1000.0
                            if on_ocr_timing:
                                try:
                                    on_ocr_timing(dur_ms)
                                except Exception:
                                    pass
                            completed_ocr_count += 1

            await asyncio.gather(*[_run_ocr(idx) for idx in ocr_tasks_indices])

        # Assemble chunks in strictly preserved page order
        chunks: List[Dict[str, Any]] = []
        for p_idx, entry in enumerate(page_data):
            page_num = entry["page_num"]
            text = entry["text"]
            parse_error = entry["parse_error"]

            if parse_error:
                chunks.append({
                    "page_number": page_num,
                    "heading": f"第 {page_num} 页",
                    "paragraph_ref": None,
                    "content": "",
                    "parse_error": parse_error,
                })
                if progress_callback:
                    progress_callback(
                        int(((p_idx + 1) / total_pages) * 100),
                        f"第 {page_num} 页 OCR 失败，等待人工处理"
                    )
            elif text:
                sub_texts = split_text_into_chunks(text, max_chunk_size=700)
                for sub in sub_texts:
                    chunks.append({
                        "page_number": page_num,
                        "heading": f"第 {page_num} 页",
                        "paragraph_ref": None,
                        "content": sub
                    })

            if progress_callback:
                pct = int(((p_idx + 1) / total_pages) * 100)
                progress_callback(pct, f"正在解析 PDF 第 {page_num}/{total_pages} 页...")

        return chunks
