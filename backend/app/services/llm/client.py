import json
import logging
import os
import re
import httpx
import asyncio
import time
from typing import Dict, Any, List, Optional, Callable
from app.config import settings
from app.services.http_client import HttpClientProvider, GlobalRateLimiter
from app.diagnostics import (
    call_context,
    log_diagnostic,
    sanitize_endpoint,
    extract_provider_request_id,
    stream_request_context,
)

from app.services.llm.observability import LLMCallError, safe_call_details, format_call_failure

logger = logging.getLogger(__name__)

class LLMClient:
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
            logger.warning("on_event callback error (%s): %s", event, type(cb_err).__name__)

    @classmethod
    async def chat_json(
        cls,
        system_prompt: str,
        user_prompt: str,
        max_retries: int = 2,
        on_retry: Optional[Callable[[int, float, str], Any]] = None,
        on_event: Optional[Callable[[str, Dict[str, Any]], Any]] = None
    ) -> Dict[str, Any]:
        """Call the JSON model; callbacks expose safe, observed transport stages."""
        if getattr(settings, "DEMO_MODE", False) or os.getenv("DEMO_MODE", "").lower() in {"1", "true", "yes"}:
            logger.info("DEMO_MODE enabled. Using offline fallback generator.")
            return cls._offline_fallback(system_prompt, user_prompt)
        if not settings.LLM_API_KEY:
            raise RuntimeError("LLM 未配置 API 密钥；正式模式禁止使用离线模拟结果")

        base_url = settings.LLM_BASE_URL.rstrip("/")
        model = settings.LLM_MODEL
        timeout_val = float(settings.LLM_TIMEOUT)
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt + "\n\n请必须仅以纯 JSON 格式输出结果，不要包含任何 markdown 代码块标记以外的文字。"},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.2
        }
        headers = {"Authorization": f"Bearer {settings.LLM_API_KEY}", "Content-Type": "application/json"}
        client = HttpClientProvider.get_client()
        endpoint = sanitize_endpoint(f"{base_url}/chat/completions")
        # Names verified against httpcore's asynchronous connection/http11/http2 traces.
        trace_stages = {
            "connection.connect_tcp": "connect_tcp", "connection.connect_unix_socket": "connect",
            "connection.start_tls": "tls",
            "proxy.start_tls": "tls", "socks.start_tls": "tls",
            "socks.connect_tcp": "connect_tcp", "socks.setup_socks5_connection": "connect",
            **{f"{protocol}.{event}": stage
               for protocol in ("http11", "http2")
               for event, stage in (("send_request_headers", "send_request"),
                                    ("send_request_body", "send_request"),
                                    ("receive_response_headers", "await_headers"))},
        }
        with call_context() as base_cid:
            for attempt in range(max_retries + 1):
                attempt_num = attempt + 1
                with call_context(call_id=base_cid, attempt=attempt_num):
                    started = stage_started = time.monotonic()
                    current_stage = "slot_wait"
                    status_code = provider_req_id = None
                    base = {"service": "llm", "model": model, "endpoint": endpoint,
                            "timeout_seconds": timeout_val, "attempt": attempt_num,
                            "max_attempts": max_retries + 1, "call_id": base_cid}

                    def details(**extra):
                        now = time.monotonic()
                        return safe_call_details({**base, "stage": current_stage,
                            "elapsed_ms": (now - started) * 1000,
                            "stage_elapsed_ms": (now - stage_started) * 1000,
                            "status_code": status_code, "provider_request_id": provider_req_id, **extra})

                    async def stage_event(event, stage, **extra):
                        nonlocal current_stage, stage_started
                        if stage != current_stage:
                            current_stage, stage_started = stage, time.monotonic()
                        await cls._emit(on_event, event, details(**extra))

                    async def trace(event, info):
                        # Deliberately do not read info: it contains headers, bytes, exception text.
                        stem, _, phase = event.rpartition(".")
                        stage = trace_stages.get(stem)
                        # Header/body sends share one stage. Record its first entry,
                        # not every internal completion or protocol callback.
                        if stage and ((phase == "started" and stage != current_stage)
                                      or (phase == "complete" and stage in {"connect", "connect_tcp", "tls"})):
                            await stage_event("network_stage", stage, trace_event=event)

                    await cls._emit(on_event, "slot_wait_started", details())
                    await GlobalRateLimiter.acquire()
                    try:
                        waited = time.monotonic() - started
                        await cls._emit(on_event, "slot_wait", details(waited_seconds=waited))
                        log_diagnostic("slot_wait", level=logging.DEBUG,
                            message="等待 LLM 信号量调用名额完成", duration_ms=round(waited * 1000, 2), **details())
                        await stage_event("request_start", "request_send")
                        log_diagnostic("request_dispatched", message="LLM 请求开始发送",
                            input_chars=len(system_prompt) + len(user_prompt), timeout_config=timeout_val, **details())
                        # Until real transport traces arrive, connection reuse/MockTransport cannot
                        # establish whether a TCP connection was opened for this request.
                        current_stage, stage_started = "await_headers", time.monotonic()
                        async with stream_request_context(client, "POST", f"{base_url}/chat/completions",
                            json=payload, headers=headers, timeout=timeout_val,
                            extensions={"trace": trace}) as res:
                            status_code = res.status_code
                            provider_req_id = extract_provider_request_id(res.headers)
                            await stage_event("response_headers", "await_headers")
                            log_diagnostic("response_headers", level=logging.DEBUG,
                                message="收到 LLM 响应头", duration_ms=details()["elapsed_ms"], **details())
                            await stage_event("read_body", "read_body")
                            content_bytes = await res.aread()
                            await cls._emit(on_event, "response_body", details())
                            log_diagnostic("response_body", level=logging.DEBUG,
                                message="LLM 响应体读取完成", duration_ms=details()["stage_elapsed_ms"],
                                input_bytes=len(content_bytes), **details())
                            current_stage, stage_started = "http_status", time.monotonic()
                            res.raise_for_status()
                            await stage_event("parse_response", "parse_response")
                            data = json.loads(content_bytes.decode("utf-8"))
                            content = data["choices"][0]["message"]["content"].strip()
                            if content.startswith("```"):
                                content = re.sub(r"^```(?:json)?\n?", "", content)
                                content = re.sub(r"\n?```$", "", content)
                            result = json.loads(content)
                            await cls._emit(on_event, "response_received", details())
                            log_diagnostic("response_parsed", message="LLM 响应解析成功",
                                duration_ms=details()["elapsed_ms"], **details())
                            return result
                    except Exception as exc:
                        # asyncio.CancelledError is a BaseException: cancellation is never a
                        # provider failure, never retried, and still releases the acquired slot.
                        exception_type = type(exc).__name__
                        retryable = isinstance(exc, (httpx.HTTPStatusError, httpx.TimeoutException,
                                                    httpx.NetworkError, json.JSONDecodeError))
                        if isinstance(exc, httpx.HTTPStatusError):
                            current_stage = "http_status"
                            status_code = exc.response.status_code
                            provider_req_id = extract_provider_request_id(exc.response.headers) or provider_req_id
                            retryable = status_code not in {400, 401, 403, 404, 422}
                        elif isinstance(exc, httpx.PoolTimeout):
                            current_stage, stage_started = "pool_wait", started + waited
                        elif isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError)):
                            if current_stage not in {"connect_tcp", "tls"}:
                                current_stage = "connect"
                        elif isinstance(exc, (httpx.WriteTimeout, httpx.WriteError)):
                            current_stage = "send_request"
                        will_retry = retryable and attempt < max_retries
                        failure = details(failed_stage=current_stage, exception_type=exception_type,
                                          will_retry=will_retry)
                        await cls._emit(on_event, "request_failed", failure)
                        log_diagnostic("request_failed", level=logging.WARNING if will_retry else logging.ERROR,
                            message=format_call_failure(failure), duration_ms=failure["elapsed_ms"], **failure)
                        if not will_retry:
                            # Keep a safe context on the cause chain without returning provider text.
                            raise LLMCallError(format_call_failure(failure), failure) from exc

                        if isinstance(exc, json.JSONDecodeError):
                            wait_sec, reason = 1.0, "模型返回非合规 JSON，正在重试"
                        elif isinstance(exc, httpx.HTTPStatusError):
                            wait_sec = min(15.0, 2.0 * attempt_num)
                            if status_code == 429:
                                retry_header = exc.response.headers.get("Retry-After")
                                if retry_header and retry_header.isdigit():
                                    wait_sec = min(15.0, float(retry_header))
                                await GlobalRateLimiter.report_429(wait_sec)
                                reason = f"服务商限流 (429)，等待 {wait_sec:.1f} 秒后重试"
                            else:
                                reason = f"服务临时异常 (HTTP {status_code})，等待 {wait_sec:.1f} 秒后重试"
                        else:
                            wait_sec = min(15.0, 2.0 * attempt_num)
                            reason = f"网络连接或超时 ({exception_type})，等待 {wait_sec:.1f} 秒后重试"
                        retry_info = details(failed_stage=current_stage, exception_type=exception_type,
                                             wait_seconds=wait_sec, reason=reason, will_retry=True)
                        log_diagnostic("retry_wait", level=logging.WARNING, message=reason,
                            retry_wait_seconds=wait_sec, **retry_info)
                        await cls._emit(on_event, "retry_wait", retry_info)
                        if on_retry:
                            try:
                                res_cb = on_retry(attempt_num, wait_sec, reason)
                                if asyncio.iscoroutine(res_cb):
                                    await res_cb
                            except Exception as cb_err:
                                logger.warning("on_retry callback error: %s", type(cb_err).__name__)
                        await asyncio.sleep(wait_sec)
                    finally:
                        GlobalRateLimiter.release()

    @classmethod
    def _offline_fallback(cls, system_prompt: str, user_prompt: str) -> Dict[str, Any]:
        """
        Offline deterministic generator to guarantee testability and zero crashes
        when running without cloud API keys.
        """
        user_lower = user_prompt.lower()
        
        # 1. Question extraction fallback
        if "extract_questions" in system_prompt or "提取题目" in system_prompt:
            # Simple rule-based extraction from user_prompt
            items = []
            lines = user_prompt.split("\n")
            current_stem = ""
            current_ans = ""
            current_year = None
            is_ans = False
            
            for line in lines:
                l = line.strip()
                if not l:
                    continue

                # Check for year header in line like "1998年", "2020年"
                ym = re.search(r"\b((?:19|20)\d{2})\s*年?", l)
                if ym and ("试题" in l or "真题" in l or "硕士" in l or "考试" in l or "答案" in l or "年" in l):
                    try:
                        current_year = int(ym.group(1))
                    except Exception:
                        pass

                if re.match(r"^(?:[一二三四五六七八九十]、|\d+[\.、]|（\d+）)", l):
                    if current_stem:
                        items.append({
                            "year": current_year,
                            "question_num": "识别题目",
                            "question_type": "short_answer",
                            "score": 10.0,
                            "stem": current_stem[:200],
                            "standard_answer": current_ans[:400] if current_ans else None
                        })
                    current_stem = l
                    current_ans = ""
                    is_ans = False
                elif "答案" in l or "答：" in l:
                    is_ans = True
                    current_ans += l + "\n"
                elif is_ans:
                    current_ans += l + "\n"
                else:
                    current_stem += " " + l

            if current_stem:
                items.append({
                    "year": current_year,
                    "question_num": "识别题目",
                    "question_type": "short_answer",
                    "score": 10.0,
                    "stem": current_stem[:200],
                    "standard_answer": current_ans[:400] if current_ans else None
                })
            return {"questions": items if items else [{
                "year": current_year,
                "question_num": "1",
                "question_type": "short_answer",
                "score": 10.0,
                "stem": "简述相关核心概念与特征。",
                "standard_answer": "要点包括概念界定、三大主要特征及实际应用场景。"
            }]}

        # 2. Study card generation fallback
        if "generate_study_card" in system_prompt or "学习卡" in system_prompt:
            return {
                "concise_content": "【定义】该知识点反映了专业领域的核心理论规范与实践准则。\n【核心要点】1. 明确目标导向；2. 注重结构协同；3. 实施闭环监控。\n【关键词】目标、协同、控制、闭环。",
                "detailed_content": "一、概念内涵\n该理论由权威教材所定义，强调在特定组织环境下的资源优化配置。\n\n二、主要特征与运行机制\n1. 结构化协同：各要素权责对等，分工明确。\n2. 动态适应性：根据外部环境变化及时调整反馈机制。\n3. 效能可衡量：具备明确的评估指标体系与反馈闭环。\n\n三、在历年真题中的考察要求\n历年考查重点在于要求考生结合实际场景阐述其适用边界与运行条件。",
                "answer_structure": "第一步：明确概念定义与内涵\n第二步：展开阐述三大核心特征与运行原理\n第三步：指出适用条件与边界制约\n第四步：结合实际案例进行评析（若有）",
                "confusing_points_and_cases": "【易混点辨析】与相近概念的区别在于前者侧重结构设计，后者偏向行为过程。\n【企业案例】经检索上传资料，教材及参考答案中未收录相关企业实践案例，明确标记缺失，不予凭空编造。"
            }

        # 3. Practice question generation fallback
        if "generate_practice" in system_prompt or "出题" in system_prompt:
            return {
                "questions": [
                    {
                        "stem": "请结合教材理论，简述该知识点的核心内涵、主要特征及其在实践中的适用条件。",
                        "reference_answer": "1. 概念内涵：核心定义及本质属性。\n2. 主要特征：分工明确、动态协同、指标可测。\n3. 适用条件：组织具备充足资源且环境相对稳定。",
                        "evaluation_points": [
                            {"point": "准确给出核心概念的完整定义与内涵", "score": 3.0, "basis": "教材核心定义段落"},
                            {"point": "完整阐明主要特征（至少阐述2点）", "score": 4.0, "basis": "教材特征分析章节"},
                            {"point": "准确分析适用条件与应用边界", "score": 3.0, "basis": "真题及教材适用范围"}
                        ],
                        "basis": "教材重点章节与历年高频真题考向"
                    }
                ]
            }

        # 4. Grading evaluation fallback
        if "grade_practice" in system_prompt or "批改" in system_prompt:
            # Evaluate user answer length and keywords
            user_text = user_prompt
            has_len = len(user_text.strip()) > 10
            return {
                "score_estimate": 8.0 if has_len else 2.0,
                "max_score": 10.0,
                "score_basis": "根据评分细则三项核心得分点逐项判定，覆盖了定义及部分特征。",
                "keypoints_coverage": [
                    {
                        "point": "准确给出核心概念的完整定义与内涵",
                        "status": "COVERED" if has_len else "MISSING",
                        "detail": "作答中对核心概念有清晰表述，符合教材定义要求。" if has_len else "作答未涉及核心概念定义。",
                        "basis": "参考教材第一章核心概念界定"
                    },
                    {
                        "point": "完整阐明主要特征（至少阐述2点）",
                        "status": "PARTIALLY_COVERED" if has_len else "MISSING",
                        "detail": "提到了动态协同特征，但遗漏了权责匹配要点。" if has_len else "未提及主要特征。",
                        "basis": "参考教材第二节核心特征归纳"
                    },
                    {
                        "point": "准确分析适用条件与应用边界",
                        "status": "COVERED" if len(user_text) > 40 else "MISSING",
                        "detail": "指出环境稳定为适用前提，论述正确。" if len(user_text) > 40 else "缺少适用条件的阐述。",
                        "basis": "历年真题标准答案参考"
                    }
                ],
                "conceptual_errors": [] if has_len else ["缺少必要理论术语，概念界定模糊"],
                "ungrounded_claims": [],
                "expression_rewrite": "建议答题时先用一句话亮出标准定义，再以‘一、特征... 二、条件...’结构化分条展开，强化考场卷面表现力。"
            }

        # 5. Question-Answer assistant fallback
        if "answer_assistant" in system_prompt or "解题思路" in system_prompt:
            return {
                "thought_process": "本题考查核心概念与实际运用。第一步定义术语，第二步拆解核心要素，第三步结合题目场景作结论。",
                "ai_answer": "一、概念定义：该理论是指组织在特定约束下实现目标的规范与机制。\n二、核心要点：1. 权责对等；2. 流程闭环；3. 弹性调控。\n三、总结：考场答题应突出关键词并条理分明。",
                "conflict_notes": None
            }

        # 6. Knowledge point extraction fallback
        if "考纲考点梳理" in system_prompt or "梳理并提取所有核心知识点" in system_prompt or "candidates" in system_prompt:
            chunk_matches = re.findall(r"\[切片ID:\s*([a-zA-Z0-9_-]+)\]", user_prompt)
            candidates = []
            if chunk_matches:
                # generate candidate referencing existing chunk_id
                c_id = chunk_matches[0]
                candidates.append({
                    "name": "管理职能与控制过程",
                    "chapter": "第一章 管理概述",
                    "definition": "管理职能是指管理者在管理活动中应当履行的基本职责与控制反馈流程。",
                    "source_chunk_ids": [c_id],
                    "quote": "管理活动包括计划、组织、领导和控制等基本职能。"
                })
            return {"candidates": candidates}

        return {"status": "ok", "message": "Success"}

