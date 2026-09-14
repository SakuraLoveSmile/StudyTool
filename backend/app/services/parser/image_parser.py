import logging
from pathlib import Path
from typing import List, Dict, Any, Callable, Optional
from app.services.parser.ocr_service import OCRService, OCRServiceError
from app.diagnostics import unit_context, log_diagnostic

logger = logging.getLogger(__name__)

class ImageParser:
    @classmethod
    async def parse_image(
        cls, 
        file_path: Path, 
        progress_callback: Optional[Callable[[int, str], None]] = None,
        on_ocr_event: Optional[Callable[[str, dict], Any]] = None,
        on_ocr_finish: Optional[Callable[[bool, Optional[str]], None]] = None
    ) -> List[Dict[str, Any]]:
        """Parses an image file (JPG/PNG) via OCR.

        on_ocr_event: unified OCR request lifecycle callback (slot_wait /
        request_start / response_received / retry_wait); on_ocr_finish(ok, error)
        marks the single-image OCR unit terminal.
        """
        if progress_callback:
            progress_callback(20, f"正在读取图片 {file_path.name}...")

        suffix = file_path.suffix.lower().replace(".", "")
        if suffix in ["jpg", "jpeg"]:
            fmt = "jpeg"
        else:
            fmt = "png"

        with open(file_path, "rb") as f:
            img_bytes = f.read()

        if progress_callback:
            progress_callback(50, f"正在进行 OCR 识别...")

        unit_id = "ocr-image-single"
        scope_str = "整图 OCR"
        with unit_context(unit_id=unit_id, scope=scope_str):
            log_diagnostic(
                "unit_started",
                level=logging.INFO,
                message="单图 OCR 识别开始",
                stage="ocr",
                unit_id=unit_id,
                scope=scope_str
            )
            parse_error = None
            try:
                text = await OCRService.extract_text_from_image_bytes(img_bytes, fmt, on_event=on_ocr_event)
                if on_ocr_finish:
                    on_ocr_finish(True, None)
                log_diagnostic(
                    "unit_completed",
                    level=logging.INFO,
                    message="单图 OCR 识别完成",
                    stage="ocr",
                    unit_id=unit_id,
                    scope=scope_str
                )
            except OCRServiceError as exc:
                text = ""
                parse_error = str(exc)
                if on_ocr_finish:
                    try:
                        on_ocr_finish(False, str(exc))
                    except Exception:
                        pass
                log_diagnostic(
                    "unit_failed",
                    level=logging.ERROR,
                    message=f"单图 OCR 识别失败: {str(exc)}",
                    stage="ocr",
                    unit_id=unit_id,
                    scope=scope_str,
                    exception_type=type(exc).__name__
                )
        
        if progress_callback:
            progress_callback(100, f"OCR 识别完成")

        return [{
            "page_number": None,
            "heading": f"图片: {file_path.name}",
            "paragraph_ref": f"整图 OCR",
            "content": text or "",
            "parse_error": parse_error,
        }]
