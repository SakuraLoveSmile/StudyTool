import asyncio
import base64
import json
import logging
import httpx
import os
import time
from typing import Optional, Callable, Dict, Any
from app.config import settings
from app.services.http_client import HttpClientProvider, GlobalRateLimiter
from app.diagnostics import (
    call_context,
    log_diagnostic,
    sanitize_endpoint,
    extract_provider_request_id,
    stream_request_context,
    extract_safe_exception_info,
)

logger = logging.getLogger(__name__)


class OCRServiceError(RuntimeError):
    """Raised when OCR cannot produce trustworthy text."""

class OCRQuotaExhaustedError(OCRServiceError):
    """Raised when OCR quota or rate limit is exhausted (HTTP 429)."""

class OCRService:
    @classmethod
    async def _emit(cls, on_event, event: str, info: Optional[Dict[str, Any]] = None):
        """Invokes the unified lifecycle callback (sync or async), never raising."""
        if on_event is None:
            return
        try:
            res = on_event(event, info or {})
            if asyncio.iscoroutine(res):
                await res
        except Exception as cb_err:
            logger.warning("OCR on_event callback error (%s): %s", event, cb_err)

    @classmethod
    async def extract_text_from_image_bytes(
        cls,
        image_bytes: bytes,
        image_format: str = "png",
        on_event: Optional[Callable[[str, Dict[str, Any]], Any]] = None
    ) -> str:
        """Extract text from image bytes via configured OCR adapter.

        on_event receives the same unified lifecycle as LLMClient:
        slot_wait / request_start / response_received / retry_wait.
        """
        mode = settings.OCR_MODE.lower().strip()
        
        if mode == "vision_llm":
            return await cls._extract_via_vision_llm(image_bytes, image_format, on_event)
        elif mode == "paddleocr_api":
            return await cls._extract_via_paddleocr_api(image_bytes, image_format, on_event)
        elif mode == "mock" and (
            getattr(settings, "DEMO_MODE", False)
            or os.getenv("DEMO_MODE", "").lower() in {"1", "true", "yes"}
        ):
            logger.info("OCR running in explicitly enabled demo mode")
            return "[演示模式 OCR 文本，占位内容，不可用于正式资料]"
        else:
            raise OCRServiceError(
                "OCR 未配置可用服务；请配置 OCR_MODE=paddleocr_api 或 OCR_MODE=vision_llm，或仅在演示环境显式开启 DEMO_MODE"
            )

    @classmethod
    async def _extract_via_vision_llm(
        cls,
        image_bytes: bytes,
        image_format: str,
        on_event: Optional[Callable[[str, Dict[str, Any]], Any]] = None
    ) -> str:
        api_key = settings.OCR_API_KEY or settings.LLM_API_KEY
        base_url = (settings.OCR_BASE_URL or settings.LLM_BASE_URL).rstrip("/")
        model = settings.OCR_VISION_MODEL or settings.LLM_MODEL
        
        if not api_key:
            raise OCRServiceError("OCR 未配置 API 密钥")
            
        b64_img = base64.b64encode(image_bytes).decode("utf-8")
        data_uri = f"data:image/{image_format};base64,{b64_img}"
        
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "请精确识别并按原格式输出该图片/扫描页中的所有文字内容，包括标题、题号、正文。无需添加任何解释。"},
                        {"type": "image_url", "image_url": {"url": data_uri}}
                    ]
                }
            ],
            "max_tokens": 4096,
            "temperature": 0.0
        }
        
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        client = HttpClientProvider.get_client()
        endpoint = sanitize_endpoint(f"{base_url}/chat/completions")
        input_bytes = len(image_bytes)
        timeout_val = float(settings.LLM_TIMEOUT)

        with call_context() as base_cid:
            for attempt in range(3):
                attempt_num = attempt + 1
                with call_context(call_id=base_cid, attempt=attempt_num):
                    t_slot = time.monotonic()
                    await GlobalRateLimiter.acquire()
                    slot_waited = time.monotonic() - t_slot
                    await cls._emit(on_event, "slot_wait", {"waited_seconds": round(slot_waited, 3), "attempt": attempt_num})
                    log_diagnostic(
                        "slot_wait",
                        level=logging.DEBUG,
                        message=f"等待 OCR 信号量名额完成 ({slot_waited:.3f}s)",
                        service="ocr",
                        model=model,
                        duration_ms=round(slot_waited * 1000.0, 2),
                        attempt=attempt_num
                    )

                    current_stage = "request_send"
                    status_code = None
                    provider_req_id = None
                    try:
                        await cls._emit(on_event, "request_start", {"attempt": attempt_num})
                        log_diagnostic(
                            "request_dispatched",
                            level=logging.INFO,
                            message="OCR 请求已发送",
                            service="ocr",
                            model=model,
                            endpoint=endpoint,
                            input_bytes=input_bytes,
                            timeout_config=timeout_val,
                            attempt=attempt_num
                        )

                        t_req_start = time.monotonic()
                        current_stage = "await_headers"

                        async with stream_request_context(
                            client,
                            "POST",
                            f"{base_url}/chat/completions",
                            json=payload,
                            headers=headers,
                            timeout=settings.LLM_TIMEOUT
                        ) as res:
                            headers_ms = (time.monotonic() - t_req_start) * 1000.0
                            status_code = res.status_code
                            provider_req_id = extract_provider_request_id(res.headers)
                            log_diagnostic(
                                "response_headers",
                                level=logging.DEBUG,
                                message="收到 OCR 响应头",
                                service="ocr",
                                model=model,
                                status_code=status_code,
                                provider_request_id=provider_req_id,
                                duration_ms=round(headers_ms, 2),
                                attempt=attempt_num
                            )

                            current_stage = "read_body"
                            t_body_start = time.monotonic()
                            body_bytes = await res.aread()
                            body_ms = (time.monotonic() - t_body_start) * 1000.0
                            log_diagnostic(
                                "response_body",
                                level=logging.DEBUG,
                                message="OCR 响应体读取完成",
                                service="ocr",
                                model=model,
                                status_code=status_code,
                                provider_request_id=provider_req_id,
                                duration_ms=round(body_ms, 2),
                                input_bytes=len(body_bytes),
                                attempt=attempt_num
                            )

                            current_stage = "http_status"
                            res.raise_for_status()

                            current_stage = "parse_response"
                            t_parse_start = time.monotonic()
                            data = json.loads(body_bytes.decode("utf-8"))
                            content = data["choices"][0]["message"]["content"].strip()
                            if not content:
                                raise OCRServiceError("OCR 服务返回空文本")

                            total_req_ms = (time.monotonic() - t_req_start) * 1000.0
                            await cls._emit(on_event, "response_received", {})
                            log_diagnostic(
                                "response_parsed",
                                level=logging.INFO,
                                message="OCR 识别成功",
                                service="ocr",
                                model=model,
                                status_code=status_code,
                                provider_request_id=provider_req_id,
                                duration_ms=round(total_req_ms, 2),
                                attempt=attempt_num
                            )
                            return content

                    except OCRServiceError as exc:
                        log_diagnostic(
                            "request_failed",
                            level=logging.ERROR,
                            message=str(exc),
                            service="ocr",
                            model=model,
                            endpoint=endpoint,
                            status_code=status_code,
                            provider_request_id=provider_req_id,
                            failed_stage="parse_response",
                            exception_type="OCRServiceError",
                            attempt=attempt_num
                        )
                        raise
                    except httpx.HTTPStatusError as exc:
                        failed_stage = "http_status"
                        status_code = exc.response.status_code
                        provider_req_id = extract_provider_request_id(exc.response.headers) or provider_req_id

                        if status_code in {400, 401, 403, 404, 422} or attempt == 2:
                            log_diagnostic(
                                "request_failed",
                                level=logging.ERROR,
                                message=f"OCR 请求失败 (HTTP {status_code})",
                                service="ocr",
                                model=model,
                                endpoint=endpoint,
                                status_code=status_code,
                                provider_request_id=provider_req_id,
                                failed_stage=failed_stage,
                                exception_type="HTTPStatusError",
                                attempt=attempt_num
                            )
                            raise OCRServiceError(f"OCR 请求失败（HTTP {status_code}）") from exc

                        if status_code == 429:
                            await GlobalRateLimiter.report_429(5.0)
                            reason = "OCR 服务限流 (429)，等待 5.0 秒后重试"
                        else:
                            reason = f"OCR 服务临时异常 (HTTP {status_code})，等待后重试"

                        log_diagnostic(
                            "retry_wait",
                            level=logging.WARNING,
                            message=reason,
                            service="ocr",
                            model=model,
                            endpoint=endpoint,
                            status_code=status_code,
                            provider_request_id=provider_req_id,
                            failed_stage=failed_stage,
                            exception_type="HTTPStatusError",
                            retry_reason=reason,
                            retry_wait_seconds=5.0,
                            attempt=attempt_num
                        )
                        await cls._emit(on_event, "retry_wait", {
                            "attempt": attempt_num,
                            "wait_seconds": 5.0,
                            "reason": reason
                        })
                        logger.warning("OCR transient HTTP failure, retry %d/2", attempt_num)
                        await asyncio.sleep(5.0)

                    except (httpx.TimeoutException, httpx.NetworkError) as exc:
                        if isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError)):
                            failed_stage = "connect"
                        else:
                            failed_stage = current_stage

                        exc_name = type(exc).__name__
                        if attempt == 2:
                            log_diagnostic(
                                "request_failed",
                                level=logging.ERROR,
                                message=f"OCR 网络请求失败: {exc_name}",
                                service="ocr",
                                model=model,
                                endpoint=endpoint,
                                status_code=status_code,
                                provider_request_id=provider_req_id,
                                failed_stage=failed_stage,
                                exception_type=exc_name,
                                attempt=attempt_num
                            )
                            raise OCRServiceError(f"OCR 网络请求失败: {exc_name}") from exc

                        reason = f"OCR 网络连接或超时 ({exc_name})，等待后重试"
                        log_diagnostic(
                            "retry_wait",
                            level=logging.WARNING,
                            message=reason,
                            service="ocr",
                            model=model,
                            endpoint=endpoint,
                            status_code=status_code,
                            provider_request_id=provider_req_id,
                            failed_stage=failed_stage,
                            exception_type=exc_name,
                            retry_reason=reason,
                            retry_wait_seconds=5.0,
                            attempt=attempt_num
                        )
                        await cls._emit(on_event, "retry_wait", {
                            "attempt": attempt_num,
                            "wait_seconds": 5.0,
                            "reason": reason
                        })
                        logger.warning("OCR transient network failure, retry %d/2", attempt_num)
                        await asyncio.sleep(5.0)

                    except Exception as exc:
                        failed_stage = current_stage
                        exc_name = type(exc).__name__
                        log_diagnostic(
                            "request_failed",
                            level=logging.ERROR,
                            message=f"OCR 识别失败: {exc_name}",
                            service="ocr",
                            model=model,
                            endpoint=endpoint,
                            failed_stage=failed_stage,
                            exception_type=exc_name,
                            attempt=attempt_num
                        )
                        logger.error("Vision OCR response parsing failed: %s", exc_name)
                        raise OCRServiceError(f"OCR 识别失败: {exc_name}") from exc
                    finally:
                        GlobalRateLimiter.release()

        raise OCRServiceError("OCR 请求失败")

    @classmethod
    async def _extract_via_paddleocr_api(
        cls,
        image_bytes: bytes,
        image_format: str,
        on_event: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
        override_api_url: Optional[str] = None,
        override_token: Optional[str] = None,
    ) -> str:
        api_url = (override_api_url or settings.BAIDU_OCR_API_URL or "").strip()
        token = (override_token or settings.BAIDU_OCR_TOKEN or "").strip()

        if not api_url or not token:
            raise OCRServiceError("百度 OCR 未配置完整 API URL 或 Token")

        # Never append /chat/completions; use the exact api_url provided.
        endpoint_clean = sanitize_endpoint(api_url)
        b64_file = base64.b64encode(image_bytes).decode("utf-8")

        payload = {
            "file": b64_file,
            "fileType": 1,
            "visualize": False,
        }
        headers = {
            "Authorization": f"token {token}",
            "Content-Type": "application/json",
        }

        client = HttpClientProvider.get_client()
        max_retries = 2
        attempt = 0
        backoff_delays = [1.0, 2.0]

        while True:
            attempt += 1
            call_id = f"ocr-baidu-{int(time.monotonic() * 1000)}-{attempt}"

            # 1. slot_wait
            await cls._emit(on_event, "slot_wait", {"attempt": attempt, "service": "ocr"})
            await GlobalRateLimiter.acquire()
            t_start = time.monotonic()
            try:
                # 2. request_start
                await cls._emit(on_event, "request_start", {
                    "attempt": attempt,
                    "service": "ocr",
                    "endpoint": endpoint_clean,
                })

                with call_context(call_id=call_id, attempt=attempt):
                    try:
                        async with stream_request_context(
                            client,
                            "POST",
                            api_url,
                            json=payload,
                            headers=headers,
                            timeout=60.0,
                        ) as res:
                            status_code = res.status_code
                            dur_ms = (time.monotonic() - t_start) * 1000.0

                            await cls._emit(on_event, "response_received", {
                                "attempt": attempt,
                                "service": "ocr",
                                "status_code": status_code,
                                "duration_ms": dur_ms,
                            })

                            if status_code == 200:
                                res_bytes = await res.aread()
                                try:
                                    data = json.loads(res_bytes.decode("utf-8"))
                                except Exception as json_err:
                                    raise OCRServiceError(f"百度 OCR 响应 JSON 解析失败: {json_err}")

                                if "errorCode" not in data and "error_code" not in data:
                                    raise OCRServiceError("百度 OCR 响应缺少 errorCode 字段")

                                err_code = data.get("errorCode", data.get("error_code"))
                                if err_code == 429:
                                    raise OCRQuotaExhaustedError("百度 OCR 每日额度耗尽或限流（errorCode 429）")
                                if err_code != 0:
                                    err_msg = data.get("errorMsg", data.get("error_msg", "未知业务错误"))
                                    raise OCRServiceError(f"百度 OCR 返回错误（{err_code}）: {err_msg}")

                                result = data.get("result")
                                if not result or not isinstance(result, dict):
                                    raise OCRServiceError("百度 OCR 响应缺少 result 对象")

                                ocr_results = result.get("ocrResults")
                                if not isinstance(ocr_results, list) or len(ocr_results) != 1:
                                    count_str = len(ocr_results) if isinstance(ocr_results, list) else type(ocr_results).__name__
                                    raise OCRServiceError(f"百度 OCR 单页识别结果数量异常（必须恰好为 1，实际为 {count_str}）")

                                single_page = ocr_results[0]
                                if not isinstance(single_page, dict):
                                    raise OCRServiceError("百度 OCR ocrResults[0] 结构异常，必须为对象")

                                pruned = single_page.get("prunedResult")
                                if not isinstance(pruned, dict):
                                    raise OCRServiceError("百度 OCR 响应缺少 prunedResult 结构")

                                rec_texts = pruned.get("rec_texts")
                                if not isinstance(rec_texts, list):
                                    raise OCRServiceError("百度 OCR rec_texts 必须为数组")

                                lines = []
                                for item in rec_texts:
                                    if not isinstance(item, str):
                                        raise OCRServiceError(f"百度 OCR rec_texts 包含非字符串元素: {type(item).__name__}")
                                    cleaned = item.strip()
                                    if cleaned:
                                        lines.append(cleaned)

                                if not lines:
                                    raise OCRServiceError("百度 OCR 识别文本为空")

                                return "\n".join(lines)

                            if status_code == 429:
                                raise OCRQuotaExhaustedError("百度 OCR 每日额度耗尽或触发限流（HTTP 429）")

                            if status_code in {403, 413, 422}:
                                body_err = await res.aread()
                                err_summary = body_err.decode("utf-8", errors="replace")[:200]
                                raise OCRServiceError(f"百度 OCR 请求被拒绝（HTTP {status_code}）: {err_summary}")

                            if 400 <= status_code < 500:
                                raise OCRServiceError(f"百度 OCR 客户端错误（HTTP {status_code}）")

                            if status_code in {500, 502, 503, 504}:
                                if attempt > max_retries:
                                    raise OCRServiceError(f"百度 OCR 服务端错误重试耗尽（HTTP {status_code}）")
                                delay = backoff_delays[min(attempt - 1, len(backoff_delays) - 1)]
                                await cls._emit(on_event, "retry_wait", {
                                    "attempt": attempt,
                                    "service": "ocr",
                                    "delay": delay,
                                    "reason": f"HTTP {status_code}",
                                })
                                await asyncio.sleep(delay)
                                continue

                            raise OCRServiceError(f"百度 OCR 异常状态码（HTTP {status_code}）")

                    except (OCRQuotaExhaustedError, OCRServiceError):
                        raise
                    except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as net_err:
                        if attempt > max_retries:
                            raise OCRServiceError(f"百度 OCR 网络重试耗尽: {type(net_err).__name__}") from net_err
                        delay = backoff_delays[min(attempt - 1, len(backoff_delays) - 1)]
                        await cls._emit(on_event, "retry_wait", {
                            "attempt": attempt,
                            "service": "ocr",
                            "delay": delay,
                            "reason": type(net_err).__name__,
                        })
                        await asyncio.sleep(delay)
                        continue
                    except Exception as unk_err:
                        raise OCRServiceError(f"百度 OCR 识别未知异常: {unk_err}") from unk_err
            finally:
                GlobalRateLimiter.release()
