import asyncio
import base64
import json
import logging
import httpx
import os
import time
import unicodedata
from urllib.parse import urlsplit
from typing import Optional, Callable, Dict, Any
from app.config import settings
from app.services.http_client import HttpClientProvider, GlobalRateLimiter
from app.diagnostics import (
    call_context,
    log_diagnostic,
    sanitize_endpoint,
    sanitize_text,
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
    @staticmethod
    def _valid_base_url(value: str) -> bool:
        try:
            parsed = urlsplit(value)
            # Access port too: malformed/out-of-range ports must fail before HTTP.
            parsed.port
            return bool(
                parsed.scheme in {"http", "https"}
                and parsed.hostname
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
                and "\\" not in value
                and not any(ch.isspace() or unicodedata.category(ch).startswith("C") for ch in value)
            )
        except ValueError:
            return False

    @classmethod
    def _vision_configuration(cls) -> Dict[str, str]:
        """Resolve credentials as a pair; never send a shared secret to a dedicated host."""
        ocr_url = settings.OCR_BASE_URL.strip()
        ocr_key = settings.OCR_API_KEY.strip()
        dedicated = bool(ocr_url or ocr_key)
        base_url = ocr_url if dedicated else settings.LLM_BASE_URL.strip()
        api_key = ocr_key if dedicated else settings.LLM_API_KEY.strip()
        model = settings.OCR_VISION_MODEL.strip()
        message = ""
        if dedicated and (not ocr_url or not ocr_key):
            message = "通用 OCR 独立配置不完整，请同时设置 OCR_BASE_URL 和 OCR_API_KEY"
        elif not base_url or not cls._valid_base_url(base_url):
            message = "通用 OCR 的 Base URL 必须为有效 HTTP/HTTPS 地址，不能包含账号、查询参数或片段"
        elif not api_key or any(not 33 <= ord(ch) <= 126 for ch in api_key):
            message = "通用 OCR 未配置有效 API Key"
        elif not model or any(ch.isspace() or unicodedata.category(ch).startswith("C") for ch in model):
            message = "通用 OCR 未配置有效视觉模型 OCR_VISION_MODEL"
        elif settings.OCR_TIMEOUT <= 0:
            message = "通用 OCR 超时 OCR_TIMEOUT 必须大于 0 秒"
        return {
            "base_url": base_url.rstrip("/"), "api_key": api_key, "model": model,
            "source": "dedicated" if dedicated else "llm_shared", "message": message,
        }

    @classmethod
    def configuration_status(cls) -> Dict[str, Any]:
        """Describe configuration completeness without network calls or secret values."""
        mode = settings.OCR_MODE.lower().strip()
        status = {
            "mode": mode, "provider": "", "base_url": "", "model": "",
            "configured": False, "message": "", "source": "",
        }
        if mode == "vision_llm":
            config = cls._vision_configuration()
            status.update(
                provider="OpenAI 兼容视觉接口", base_url=sanitize_endpoint(config["base_url"]),
                model=config["model"], source=config["source"],
                configured=not bool(config["message"]),
                message=config["message"] or "通用 OCR 配置完整，尚未测试实际识别",
            )
        elif mode == "paddleocr_api":
            url = settings.BAIDU_OCR_API_URL.strip()
            token = settings.BAIDU_OCR_TOKEN.strip()
            complete = bool(url and token and cls._valid_base_url(url))
            status.update(
                provider="百度 PaddleOCR", base_url=sanitize_endpoint(url),
                model=cls.JOBS_API_MODEL, source="dedicated", configured=complete,
                message="百度 OCR 配置完整，尚未测试实际识别" if complete else "百度 OCR 未配置有效 HTTP/HTTPS API 地址和 Token",
            )
        elif mode == "mock":
            status["message"] = "当前为演示 OCR，不能用于正式资料识别"
        else:
            status["message"] = "OCR 模式不可用，请选择 vision_llm 或 paddleocr_api"
        # Even a credential accidentally entered in another setting must not appear.
        for key, value in status.items():
            if isinstance(value, str):
                for secret in (settings.OCR_API_KEY, settings.LLM_API_KEY, settings.BAIDU_OCR_TOKEN):
                    if secret.strip():
                        value = value.replace(secret.strip(), "[REDACTED]")
                status[key] = sanitize_text(value)
        return status

    @staticmethod
    def _vision_response_text(body: bytes) -> str:
        try:
            data = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise OCRServiceError("OCR 服务返回非 JSON 或畸形响应") from exc
        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise OCRServiceError("OCR 服务响应缺少有效 choices")
        choice = choices[0]
        if choice.get("finish_reason") == "length":
            raise OCRServiceError("OCR 识别结果被截断，请提高输出限制或缩小单页内容后重试")
        message = choice.get("message")
        if not isinstance(message, dict):
            raise OCRServiceError("OCR 服务响应缺少有效 message")
        if message.get("refusal") or choice.get("finish_reason") == "content_filter":
            raise OCRServiceError("OCR 服务拒绝识别该图片")
        content = message.get("content")
        if isinstance(content, list):
            if any(isinstance(block, dict) and block.get("type") == "refusal" for block in content):
                raise OCRServiceError("OCR 服务拒绝识别该图片")
            content = "\n".join(
                block["text"] for block in content
                if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
            )
        if not isinstance(content, str) or not content.strip():
            raise OCRServiceError("OCR 服务返回空文本或无有效文本内容")
        return content.strip()

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
        on_event: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
        *,
        max_attempts: int = 3,
    ) -> str:
        """Extract text from image bytes via configured OCR adapter.

        on_event receives the same unified lifecycle as LLMClient:
        slot_wait / request_start / response_received / retry_wait.
        """
        mode = settings.OCR_MODE.lower().strip()
        
        if mode == "vision_llm":
            return await cls._extract_via_vision_llm(image_bytes, image_format, on_event, max_attempts=max_attempts)
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
        on_event: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
        *,
        max_attempts: int = 3,
    ) -> str:
        config = cls._vision_configuration()
        if config["message"]:
            raise OCRServiceError(config["message"])
        if not isinstance(max_attempts, int) or max_attempts < 1:
            raise OCRServiceError("OCR 请求尝试次数必须至少为 1")
        api_key, base_url, model = config["api_key"], config["base_url"], config["model"]
            
        b64_img = base64.b64encode(image_bytes).decode("utf-8")
        fmt = image_format.lower().lstrip(".")
        mime = "image/jpeg" if fmt in {"jpg", "jpeg"} else f"image/{fmt}"
        data_uri = f"data:{mime};base64,{b64_img}"
        # This model's documented task token selects OCR rather than another
        # document-recognition task. Other vision models use the general prompt.
        prompt = "OCR:" if model == "PaddlePaddle/PaddleOCR-VL-1.5" else "请精确识别并按原格式输出该图片/扫描页中的所有文字内容，包括标题、题号、正文。无需添加任何解释。"
        
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
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
        timeout_val = float(settings.OCR_TIMEOUT)

        with call_context() as base_cid:
            for attempt in range(max_attempts):
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
                            timeout=timeout_val
                        ) as res:
                            headers_ms = (time.monotonic() - t_req_start) * 1000.0
                            status_code = res.status_code
                            provider_req_id = extract_provider_request_id(res.headers)
                            if provider_req_id:
                                provider_req_id = sanitize_text(provider_req_id.replace(api_key, "[REDACTED]"))
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
                            content = cls._vision_response_text(body_bytes)

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
                        if provider_req_id:
                            provider_req_id = sanitize_text(provider_req_id.replace(api_key, "[REDACTED]"))

                        if status_code in {400, 401, 403, 404, 422} or attempt_num == max_attempts:
                            summary = cls._jobs_safe_error_summary(body_bytes, api_key)
                            error_message = f"OCR 请求失败（HTTP {status_code}）"
                            reason = summary.get("detail", "")
                            if summary.get("reason_code"):
                                reason += f"（code={summary['reason_code']}）"
                            if reason:
                                error_message += f"：{reason}"
                            log_diagnostic(
                                "request_failed",
                                level=logging.ERROR,
                                message=error_message,
                                service="ocr",
                                model=model,
                                endpoint=endpoint,
                                status_code=status_code,
                                provider_request_id=provider_req_id,
                                failed_stage=failed_stage,
                                exception_type="HTTPStatusError",
                                attempt=attempt_num
                            )
                            raise OCRServiceError(error_message) from exc

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
                        logger.warning("OCR transient HTTP failure, retry %d/%d", attempt_num, max_attempts - 1)
                        await asyncio.sleep(5.0)

                    except (httpx.TimeoutException, httpx.NetworkError) as exc:
                        if isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError)):
                            failed_stage = "connect"
                        else:
                            failed_stage = current_stage

                        exc_name = type(exc).__name__
                        if attempt_num == max_attempts:
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
                        logger.warning("OCR transient network failure, retry %d/%d", attempt_num, max_attempts - 1)
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

    # ------------------------------------------------------------------
    # Baidu PP-OCRv6 asynchronous jobs API (submit → poll → JSONL download).
    # Wire contract verified against the official PaddleOCR API SDK
    # (paddleocr/_api_client/_http.py, _core.py, _poller.py):
    #   POST {jobs_url}  multipart(model, optionalPayload, file)  Bearer token
    #   GET  {jobs_url}/{jobId}                                   Bearer token
    #   GET  {resultUrl.jsonUrl}                                  pre-signed, NO token
    # ------------------------------------------------------------------
    JOBS_API_MODEL = "PP-OCRv6"
    JOB_POLL_INTERVAL = 5.0        # seconds between two job-status polls
    JOB_TOTAL_TIMEOUT = 300.0      # hard cap for submit + poll + download of one page
    JOB_REQUEST_TIMEOUT = 60.0     # hard cap for a single HTTP request
    JOB_RETRY_DELAYS = (1.0, 2.0)  # transient-failure backoff, poll/download only

    @classmethod
    def _jobs_stage_label(cls, stage: str) -> str:
        return {
            "submit": "任务提交",
            "poll": "任务状态查询",
            "download": "结果下载",
        }.get(stage, "请求")

    @classmethod
    def _jobs_check_deadline(cls, deadline: float) -> None:
        if time.monotonic() >= deadline:
            raise OCRServiceError(
                f"百度 OCR 任务超时（单页处理超过 {int(cls.JOB_TOTAL_TIMEOUT)} 秒）"
            )

    @classmethod
    def _jobs_decode_json(cls, body: bytes, what: str) -> Dict[str, Any]:
        """Decodes a JSON object response; never echoes raw payload content."""
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise OCRServiceError(f"百度 OCR {what} JSON 解析失败（{type(exc).__name__}）") from exc
        if not isinstance(payload, dict):
            raise OCRServiceError(f"百度 OCR {what}必须为 JSON 对象")
        return payload

    @classmethod
    def _jobs_unwrap_payload(cls, body: bytes, what: str) -> Dict[str, Any]:
        """Unwraps the {'code': 0, 'data': {...}} envelope used by the jobs API."""
        payload = cls._jobs_decode_json(body, what)
        code = payload.get("code")
        if code not in (0, None):
            raise OCRServiceError(f"百度 OCR {what}被拒绝（code={code}）")
        data_obj = payload.get("data")
        if isinstance(data_obj, dict):
            return data_obj
        return payload

    @classmethod
    def _jobs_safe_error_summary(cls, body: bytes, token: str) -> Dict[str, str]:
        """Read only short JSON error fields, never raw response content or credentials."""
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}

        def safe_scalar(value: Any, limit: int) -> str:
            if not isinstance(value, (str, int, float)) or isinstance(value, bool):
                return ""
            text = str(value)
            # The provider may echo the submitted token without a recognizable key.
            # Redact before truncating or normalizing so partial tokens cannot escape.
            if token:
                text = text.replace(token, "[REDACTED]")
            text = sanitize_text(text)
            text = "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in text)
            return " ".join(text.split())[:limit]

        summary: Dict[str, str] = {}
        # Baidu also returns data.errorMsg. Inspect only explicit error fields
        # there, never the rest of data (which may contain document/image output).
        roots = [payload]
        if isinstance(payload.get("data"), dict):
            roots.append(payload["data"])
        for current in roots:
            # Some providers wrap these fields in one or two `error` objects.
            for _ in range(3):
                if "reason_code" not in summary:
                    for key in ("errorCode", "code", "error_code", "type"):
                        code = safe_scalar(current.get(key), 80)
                        if code and code != "0":
                            summary["reason_code"] = code
                            break
                if "detail" not in summary:
                    for key in ("errorMsg", "message", "msg", "error"):
                        message = safe_scalar(current.get(key), 300)
                        if message:
                            summary["detail"] = message
                            break
                nested = current.get("error")
                if not isinstance(nested, dict):
                    break
                current = nested
        return summary

    @classmethod
    async def _jobs_single_request(
        cls,
        method: str,
        url: str,
        *,
        stage: str,
        attempt: int,
        on_event,
        deadline: float,
        call_id: str,
        endpoint: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        model: Optional[str] = None,
        input_bytes: Optional[int] = None,
        **kwargs: Any,
    ):
        """Performs exactly one HTTP request under the global rate limiter.

        The limiter slot is held only while a request is actually in flight; the
        5-second polling wait never occupies a slot. Returns (status, body, request_id).
        """
        client = HttpClientProvider.get_client()

        await cls._emit(on_event, "slot_wait", {"attempt": attempt, "service": "ocr", "stage": stage})
        await GlobalRateLimiter.acquire()
        try:
            cls._jobs_check_deadline(deadline)
            remaining = deadline - time.monotonic()
            timeout = min(cls.JOB_REQUEST_TIMEOUT, max(remaining, 0.1))

            with call_context(call_id=call_id, attempt=attempt):
                await cls._emit(on_event, "request_start", {
                    "attempt": attempt,
                    "service": "ocr",
                    "stage": stage,
                })
                log_diagnostic(
                    "request_dispatched",
                    level=logging.INFO,
                    message=f"百度 OCR 请求已发送（{cls._jobs_stage_label(stage)}）",
                    service="ocr",
                    model=model,
                    endpoint=endpoint,
                    input_bytes=input_bytes,
                    timeout_config=round(timeout, 2),
                    attempt=attempt,
                    stage=stage,
                )

                t_req = time.monotonic()
                async with stream_request_context(
                    client, method, url, headers=headers, timeout=timeout, **kwargs
                ) as res:
                    status_code = int(res.status_code)
                    provider_req_id = extract_provider_request_id(res.headers)
                    body = await res.aread()
                duration_ms = (time.monotonic() - t_req) * 1000.0

                log_diagnostic(
                    "response_received",
                    level=logging.DEBUG,
                    message=f"百度 OCR 响应已读取（{cls._jobs_stage_label(stage)}）",
                    service="ocr",
                    model=model,
                    endpoint=endpoint,
                    status_code=status_code,
                    provider_request_id=provider_req_id,
                    duration_ms=round(duration_ms, 2),
                    attempt=attempt,
                    stage=stage,
                )
                await cls._emit(on_event, "response_received", {
                    "attempt": attempt,
                    "service": "ocr",
                    "stage": stage,
                    "status_code": status_code,
                    "duration_ms": round(duration_ms, 2),
                })
                return status_code, body, provider_req_id
        finally:
            GlobalRateLimiter.release()

    @classmethod
    async def _jobs_request_with_retries(
        cls,
        method: str,
        url: str,
        *,
        stage: str,
        on_event,
        deadline: float,
        call_id: str,
        endpoint: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        model: Optional[str] = None,
        **kwargs: Any,
    ) -> bytes:
        """GET with up to 2 retries (1s, 2s) for transient failures; returns the 2xx body."""
        max_retries = len(cls.JOB_RETRY_DELAYS)
        label = cls._jobs_stage_label(stage)
        attempt = 0

        while True:
            attempt += 1
            cls._jobs_check_deadline(deadline)
            transient_reason: Optional[str] = None
            try:
                status, body, _req_id = await cls._jobs_single_request(
                    method,
                    url,
                    stage=stage,
                    attempt=attempt,
                    on_event=on_event,
                    deadline=deadline,
                    call_id=call_id,
                    endpoint=endpoint,
                    headers=headers,
                    model=model,
                    **kwargs,
                )
            except asyncio.CancelledError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                transient_reason = type(exc).__name__
            else:
                if 200 <= status < 300:
                    return body
                if status == 429:
                    raise OCRQuotaExhaustedError(f"百度 OCR 每日额度耗尽或触发限流（HTTP {status}）")
                if status >= 500:
                    transient_reason = f"HTTP {status}"
                else:
                    raise OCRServiceError(f"百度 OCR {label}失败（HTTP {status}）")

            if attempt > max_retries:
                raise OCRServiceError(f"百度 OCR {label}重试耗尽（{transient_reason}）")

            delay = cls.JOB_RETRY_DELAYS[attempt - 1]
            reason = f"百度 OCR {label}暂时性故障（{transient_reason}），{delay:.0f} 秒后重试"
            log_diagnostic(
                "retry_wait",
                level=logging.WARNING,
                message=reason,
                service="ocr",
                model=model,
                endpoint=endpoint,
                failed_stage=stage,
                exception_type=transient_reason,
                retry_reason=transient_reason,
                retry_wait_seconds=delay,
                attempt=attempt,
                stage=stage,
            )
            await cls._emit(on_event, "retry_wait", {
                "attempt": attempt,
                "service": "ocr",
                "stage": stage,
                "wait_seconds": delay,
                "reason": transient_reason,
            })
            logger.warning("Baidu OCR %s transient failure (%s), retry %d/%d", stage, transient_reason, attempt, max_retries)
            await asyncio.sleep(delay)

    @classmethod
    async def _jobs_submit(
        cls,
        jobs_url: str,
        token: str,
        image_bytes: bytes,
        image_format: str,
        on_event,
        deadline: float,
        call_id: str,
        endpoint: Optional[str],
    ) -> str:
        """Submits one multipart job. Never retried: a retry would create a duplicate job."""
        fmt = (image_format or "png").lower().lstrip(".") or "png"
        content_type = "image/jpeg" if fmt in {"jpg", "jpeg"} else f"image/{fmt}"
        optional_payload = {
            "useDocOrientationClassify": False,
            "useDocUnwarping": False,
            "useTextlineOrientation": False,
            "visualize": False,
        }
        data = {
            "model": cls.JOBS_API_MODEL,
            "optionalPayload": json.dumps(optional_payload),
        }
        files = {"file": (f"page.{fmt}", image_bytes, content_type)}
        headers = {"Authorization": f"Bearer {token}"}

        status, body, _req_id = await cls._jobs_single_request(
            "POST",
            jobs_url,
            stage="submit",
            attempt=1,
            on_event=on_event,
            deadline=deadline,
            call_id=call_id,
            endpoint=endpoint,
            headers=headers,
            model=cls.JOBS_API_MODEL,
            input_bytes=len(image_bytes),
            data=data,
            files=files,
        )

        http_ok = 200 <= status < 300
        payload = cls._jobs_decode_json(body, "提交响应") if http_ok else {}
        if not http_ok or payload.get("code") not in (0, None):
            summary = cls._jobs_safe_error_summary(body, token)
            message = f"百度 OCR 任务提交失败（HTTP {status}）"
            reason = summary.get("detail", "")
            if summary.get("reason_code"):
                reason = f"{reason}（code={summary['reason_code']}）" if reason else f"code={summary['reason_code']}"
            if reason:
                message += f"：{reason}"
            with call_context(call_id=call_id, attempt=1):
                log_diagnostic(
                    "request_failed",
                    level=logging.WARNING,
                    message=message,
                    service="ocr",
                    model=cls.JOBS_API_MODEL,
                    endpoint=endpoint,
                    status_code=status,
                    provider_request_id=sanitize_text((_req_id or "").replace(token, "[REDACTED]")) or None,
                    failed_stage="submit",
                    stage="submit",
                    call_id=call_id,
                    attempt=1,
                    **summary,
                )
            if status == 429:
                raise OCRQuotaExhaustedError(f"百度 OCR 每日额度耗尽或触发限流（HTTP {status}）")
            raise OCRServiceError(message)

        payload = payload["data"] if isinstance(payload.get("data"), dict) else payload
        job_id = payload.get("jobId")
        if not isinstance(job_id, str) or not job_id.strip():
            raise OCRServiceError("百度 OCR 提交响应缺少 jobId")

        log_diagnostic(
            "job_submitted",
            level=logging.INFO,
            message="百度 OCR 异步任务已创建",
            service="ocr",
            model=cls.JOBS_API_MODEL,
            endpoint=endpoint,
            status_code=status,
            attempt=1,
            stage="submit",
        )
        return job_id.strip()

    @classmethod
    async def _jobs_poll_until_done(
        cls,
        jobs_url: str,
        token: str,
        job_id: str,
        on_event,
        deadline: float,
        call_id: str,
    ) -> str:
        """Polls GET {jobs_url}/{jobId} every 5s; returns the done-state JSONL URL."""
        poll_url = f"{jobs_url.rstrip('/')}/{job_id}"
        poll_endpoint = sanitize_endpoint(poll_url)
        headers = {"Authorization": f"Bearer {token}"}

        while True:
            cls._jobs_check_deadline(deadline)
            body = await cls._jobs_request_with_retries(
                "GET",
                poll_url,
                stage="poll",
                on_event=on_event,
                deadline=deadline,
                call_id=call_id,
                endpoint=poll_endpoint,
                headers=headers,
                model=cls.JOBS_API_MODEL,
            )
            data = cls._jobs_unwrap_payload(body, "任务状态响应")
            state = data.get("state")

            if state == "done":
                result_url = data.get("resultUrl")
                json_url = result_url.get("jsonUrl") if isinstance(result_url, dict) else None
                if not isinstance(json_url, str) or not json_url.strip():
                    raise OCRServiceError("百度 OCR 任务已完成但缺少 resultUrl.jsonUrl")
                log_diagnostic(
                    "job_done",
                    level=logging.INFO,
                    message="百度 OCR 异步任务已完成",
                    service="ocr",
                    model=cls.JOBS_API_MODEL,
                    endpoint=poll_endpoint,
                    stage="poll",
                )
                return json_url.strip()

            if state == "failed":
                err_msg = sanitize_text(str(data.get("errorMsg") or "未知错误"))[:200]
                log_diagnostic(
                    "job_failed",
                    level=logging.ERROR,
                    message=f"百度 OCR 异步任务失败: {err_msg}",
                    service="ocr",
                    model=cls.JOBS_API_MODEL,
                    endpoint=poll_endpoint,
                    failed_stage="poll",
                    stage="poll",
                )
                raise OCRServiceError(f"百度 OCR 任务执行失败: {err_msg}")

            if state in ("pending", "running"):
                log_diagnostic(
                    "job_poll",
                    level=logging.DEBUG,
                    message=f"百度 OCR 异步任务进行中（state={state}）",
                    service="ocr",
                    model=cls.JOBS_API_MODEL,
                    endpoint=poll_endpoint,
                    stage="poll",
                )
                remaining = deadline - time.monotonic()
                cls._jobs_check_deadline(deadline)
                await asyncio.sleep(min(cls.JOB_POLL_INTERVAL, remaining))
                continue

            raise OCRServiceError(f"百度 OCR 任务状态异常（state={state!r}）")

    @classmethod
    async def _jobs_download_jsonl(
        cls,
        json_url: str,
        on_event,
        deadline: float,
        call_id: str,
    ) -> str:
        """Downloads the pre-signed JSONL result. The URL is signed, so no token is sent."""
        body = await cls._jobs_request_with_retries(
            "GET",
            json_url,
            stage="download",
            on_event=on_event,
            deadline=deadline,
            call_id=call_id,
            endpoint=None,  # signed URL: never logged
            headers=None,   # pre-signed download must not carry the Authorization token
            model=cls.JOBS_API_MODEL,
        )
        return body.decode("utf-8", errors="replace")

    @classmethod
    def _jobs_parse_jsonl(cls, text: str) -> str:
        """Flattens result.ocrResults[].prunedResult.rec_texts into newline-joined text."""
        lines = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError as exc:
                raise OCRServiceError(f"百度 OCR 结果 JSONL 解析失败（{type(exc).__name__}）") from exc
            if not isinstance(obj, dict):
                raise OCRServiceError("百度 OCR 结果 JSONL 行必须为 JSON 对象")

            result = obj.get("result")
            if not isinstance(result, dict):
                raise OCRServiceError("百度 OCR 结果 JSONL 缺少 result 对象")
            ocr_results = result.get("ocrResults")
            if not isinstance(ocr_results, list):
                raise OCRServiceError("百度 OCR 结果 JSONL 缺少 ocrResults 数组")

            for item in ocr_results:
                pruned = item.get("prunedResult") if isinstance(item, dict) else None
                if not isinstance(pruned, dict):
                    raise OCRServiceError("百度 OCR 结果缺少 prunedResult 结构")
                rec_texts = pruned.get("rec_texts")
                if not isinstance(rec_texts, list):
                    raise OCRServiceError("百度 OCR 结果 rec_texts 必须为数组")
                for entry in rec_texts:
                    if not isinstance(entry, str):
                        raise OCRServiceError(f"百度 OCR rec_texts 包含非字符串元素: {type(entry).__name__}")
                    cleaned = entry.strip()
                    if cleaned:
                        lines.append(cleaned)

        if not lines:
            raise OCRServiceError("百度 OCR 识别文本为空")
        return "\n".join(lines)

    @classmethod
    async def _extract_via_paddleocr_api(
        cls,
        image_bytes: bytes,
        image_format: str,
        on_event: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
        override_api_url: Optional[str] = None,
        override_token: Optional[str] = None,
    ) -> str:
        """Extracts text for one page through the Baidu PP-OCRv6 asynchronous jobs API.

        Flow: multipart submit (never retried, so no duplicate jobs) → poll
        GET {jobs_url}/{jobId} every 5 seconds until state == "done" → download the
        pre-signed JSONL result (no Authorization token) → flatten
        result.ocrResults[].prunedResult.rec_texts.

        The whole page flow is capped at JOB_TOTAL_TIMEOUT (300s) and every single
        request at JOB_REQUEST_TIMEOUT (60s).
        """
        jobs_url = (override_api_url or settings.BAIDU_OCR_API_URL or "").strip()
        token = (override_token or settings.BAIDU_OCR_TOKEN or "").strip()

        if not jobs_url or not token:
            raise OCRServiceError("百度 OCR 未配置完整 API URL 或 Token")

        call_id = f"ocr-baidu-job-{int(time.monotonic() * 1000)}"
        deadline = time.monotonic() + cls.JOB_TOTAL_TIMEOUT
        endpoint_clean = sanitize_endpoint(jobs_url)

        try:
            job_id = await cls._jobs_submit(
                jobs_url,
                token,
                image_bytes,
                image_format,
                on_event,
                deadline,
                call_id,
                endpoint_clean,
            )
            json_url = await cls._jobs_poll_until_done(
                jobs_url,
                token,
                job_id,
                on_event,
                deadline,
                call_id,
            )
            jsonl_text = await cls._jobs_download_jsonl(json_url, on_event, deadline, call_id)
            return cls._jobs_parse_jsonl(jsonl_text)
        except asyncio.CancelledError:
            # Cancellation is never a business error: release and keep propagating.
            raise
        except OCRServiceError:
            raise
        except Exception as exc:
            raise OCRServiceError(f"百度 OCR 识别未知异常（{type(exc).__name__}）") from exc
