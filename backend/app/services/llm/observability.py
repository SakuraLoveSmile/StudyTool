"""Safe, shared descriptions for LLM call events (never prompts or responses)."""
import math
import re
from typing import Any, Dict, Optional

from app.diagnostics import sanitize_endpoint, sanitize_text


CALL_STAGE_LABELS = {
    "slot_wait": "等待本地调用名额", "request_send": "准备发送请求",
    "connect": "建立网络连接", "connect_tcp": "建立 TCP 连接",
    "tls": "TLS 握手", "pool_wait": "等待本地连接池",
    "send_request": "发送请求", "await_headers": "等待响应头",
    "read_body": "读取响应正文", "http_status": "检查 HTTP 状态",
    "parse_response": "解析模型结果",
}


def safe_call_details(info: Any) -> Dict[str, Any]:
    """Only copy documented scalar fields; never accept arbitrary transport info."""
    if not isinstance(info, dict):
        return {}
    result: Dict[str, Any] = {}
    for key in ("timeout_seconds", "elapsed_ms", "stage_elapsed_ms", "waited_seconds", "wait_seconds"):
        value = info.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                if math.isfinite(value) and 0 <= value <= 1e15:
                    result[key] = round(value, 3)
            except OverflowError:
                pass
    for key in ("attempt", "max_attempts", "status_code"):
        value = info.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1000000:
            result[key] = value
    for key in ("stage", "failed_stage"):
        if isinstance(info.get(key), str) and info[key] in CALL_STAGE_LABELS:
            result[key] = info[key]
    if info.get("service") == "llm":
        result["service"] = "llm"
    for key in ("model", "reason"):
        if isinstance(info.get(key), str):
            result[key] = sanitize_text(info[key])[:300]
    if isinstance(info.get("endpoint"), str):
        result["endpoint"] = sanitize_endpoint(info["endpoint"])
    for key in ("call_id", "provider_request_id", "exception_type", "error_type", "trace_event"):
        value = info.get(key)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,160}", value):
            result[key] = sanitize_text(value)
    if isinstance(info.get("will_retry"), bool):
        result["will_retry"] = info["will_retry"]
    return result


def format_call_failure(info: Dict[str, Any]) -> str:
    details = safe_call_details(info)
    stage = details.get("failed_stage", details.get("stage", "request_send"))
    error = details.get("exception_type", "UnknownError")
    status = details.get("status_code")
    kind = "超时" if error.endswith("Timeout") else "失败"
    message = f"LLM 在{CALL_STAGE_LABELS[stage]}阶段{kind}（{error}）"
    fields = []
    if details.get("model"):
        fields.append(f"模型 {details['model']}")
    if "timeout_seconds" in details:
        fields.append(f"单次网络等待上限 {details['timeout_seconds']:g} 秒")
    if "elapsed_ms" in details:
        fields.append(f"本次已耗时 {details['elapsed_ms'] / 1000:.1f} 秒")
    if details.get("attempt") and details.get("max_attempts"):
        fields.append(f"第 {details['attempt']}/{details['max_attempts']} 次")
    if status:
        fields.append(f"HTTP {status}")
    if fields:
        message += "；" + "，".join(fields)
    if stage == "await_headers" and not status:
        message += "；尚未收到 HTTP 响应，暂不能区分服务商处理与上游网络等待"
    elif stage == "read_body":
        message += "；已收到响应头，读取响应正文时中断"
    elif stage == "pool_wait":
        message += "；未取得本地连接池名额"
    elif status == 429:
        message += "；服务商限流"
    elif status in (401, 403):
        message += "；请核对服务商密钥与模型权限"
    return message


class LLMCallError(RuntimeError):
    def __init__(self, message: str, diagnostic: Dict[str, Any]):
        super().__init__(message)
        self.diagnostic = safe_call_details(diagnostic)


def get_llm_diagnostic(exc: BaseException) -> Optional[Dict[str, Any]]:
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        details = getattr(exc, "diagnostic", None)
        if isinstance(details, dict) and details.get("service") == "llm":
            return safe_call_details(details)
        exc = exc.__cause__ or exc.__context__
    return None
