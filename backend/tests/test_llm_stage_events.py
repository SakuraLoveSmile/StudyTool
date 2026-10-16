"""LLM stage evidence from isolated transports; no external provider calls."""
import asyncio
import json

import httpx
import pytest

from app.config import settings
from app.services.http_client import GlobalRateLimiter, HttpClientProvider
from app.services.llm.client import LLMClient
from app.services.llm.observability import get_llm_diagnostic, safe_call_details

SECRET = "sk-live-DO-NOT-LOG-1234567890"
BODY = "PRIVATE_PROMPT_AND_PROVIDER_BODY"


@pytest.fixture
def llm_env(monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.setattr(settings, "LLM_API_KEY", SECRET)
    monkeypatch.setattr(settings, "LLM_MODEL", "example/model")
    monkeypatch.setattr(settings, "LLM_BASE_URL", "https://user:password@example.test/v1?secret=hidden")
    monkeypatch.setattr(settings, "LLM_TIMEOUT", 60)
    slots = []
    async def acquire():
        slots.append("acquire")
    monkeypatch.setattr(GlobalRateLimiter, "acquire", acquire)
    monkeypatch.setattr(GlobalRateLimiter, "release", lambda: slots.append("release"))
    async def report_429(delay):
        pass
    monkeypatch.setattr(GlobalRateLimiter, "report_429", report_429)
    return slots


def response(request, status=200, content=None):
    return httpx.Response(status, request=request,
        headers={"x-request-id": "provider-123"},
        json=content or {"choices": [{"message": {"content": '{"answer":true}'}}]})


async def fake_trace(request, *events):
    for event in events:
        await request.extensions["trace"](event, {"headers": {"authorization": SECRET},
            "return_value": BODY, "exception": RuntimeError(SECRET)})


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,stage,trace_names", [
    (httpx.ReadTimeout, "await_headers", ("http11.receive_response_headers.started",)),
    (httpx.ConnectTimeout, "connect_tcp", ("connection.connect_tcp.started",)),
    (httpx.ConnectError, "tls", ("connection.start_tls.started",)),
    (httpx.ConnectTimeout, "tls", ("proxy.start_tls.started",)),
    (httpx.ConnectTimeout, "connect_tcp", ("socks.connect_tcp.started",)),
    (httpx.ConnectTimeout, "tls", ("socks.start_tls.started",)),
    (httpx.PoolTimeout, "pool_wait", ()),
    (httpx.WriteTimeout, "send_request", ("http2.send_request_body.started",)),
])
async def test_failure_identifies_observed_phase(llm_env, monkeypatch, kind, stage, trace_names):
    async def handler(request):
        await fake_trace(request, *trace_names)
        raise kind(f"{SECRET} {BODY}", request=request)
    events = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(HttpClientProvider, "get_client", lambda: client)
        with pytest.raises(RuntimeError) as caught:
            await LLMClient.chat_json(BODY, BODY, max_retries=0,
                on_event=lambda event, info: events.append((event, info)))
    failure = [info for event, info in events if event == "request_failed"]
    assert len(failure) == 1
    assert failure[0]["stage"] == stage
    assert failure[0]["exception_type"] == kind.__name__
    assert failure[0]["timeout_seconds"] == 60
    assert failure[0]["attempt"] == failure[0]["max_attempts"] == 1
    assert failure[0]["stage_elapsed_ms"] <= failure[0]["elapsed_ms"]
    assert failure[0]["will_retry"] is False
    assert get_llm_diagnostic(caught.value) == failure[0]
    raw = json.dumps(events) + str(caught.value)
    assert SECRET not in raw and BODY not in raw
    assert "password" not in raw and "secret=" not in raw
    assert llm_env == ["acquire", "release"]


class BrokenBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'{"partial":'
        raise httpx.ReadTimeout(SECRET)


@pytest.mark.asyncio
async def test_body_timeout_is_after_headers(llm_env, monkeypatch):
    async def handler(request):
        return httpx.Response(200, request=request, headers={"x-request-id": "provider-123"}, stream=BrokenBody())
    events = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(HttpClientProvider, "get_client", lambda: client)
        with pytest.raises(RuntimeError) as caught:
            await LLMClient.chat_json("s", "u", max_retries=0,
                on_event=lambda e, i: events.append((e, i)))
    assert [e for e, _ in events][-3:] == ["response_headers", "read_body", "request_failed"]
    diagnostic = get_llm_diagnostic(caught.value)
    assert diagnostic["stage"] == "read_body"
    assert diagnostic["status_code"] == 200
    assert diagnostic["provider_request_id"] == "provider-123"
    assert "已收到响应头" in str(caught.value)


@pytest.mark.asyncio
async def test_reused_connection_logs_only_observed_stages(llm_env, monkeypatch):
    async def handler(request):
        await fake_trace(request, "http11.send_request_headers.started", "http11.send_request_headers.complete",
            "http11.receive_response_headers.started", "http11.receive_response_headers.complete")
        await fake_trace(request, "unknown.secret.started")
        return response(request)
    events = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(HttpClientProvider, "get_client", lambda: client)
        result = await LLMClient.chat_json("s", "u", on_event=lambda e, i: events.append((e, i)))
    assert result == {"answer": True}
    names = [e for e, _ in events]
    assert names[0:3] == ["slot_wait_started", "slot_wait", "request_start"]
    assert names[-5:] == ["response_headers", "read_body", "response_body", "parse_response", "response_received"]
    assert all(info["stage"] not in {"connect_tcp", "tls"} for _, info in events)
    assert {info["call_id"] for _, info in events}.__len__() == 1
    assert SECRET not in json.dumps(events) and BODY not in json.dumps(events)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected_attempts", [(401, 1), (429, 2)])
async def test_http_failures_emit_each_attempt(llm_env, monkeypatch, status, expected_attempts):
    async def handler(request):
        return response(request, status=status, content={"error": BODY + SECRET})
    async def no_sleep(delay):
        pass
    monkeypatch.setattr("app.services.llm.client.asyncio.sleep", no_sleep)
    events = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(HttpClientProvider, "get_client", lambda: client)
        with pytest.raises(RuntimeError):
            await LLMClient.chat_json("s", "u", max_retries=1,
                on_event=lambda e, i: events.append((e, i)))
    failures = [info for e, info in events if e == "request_failed"]
    assert len(failures) == expected_attempts
    assert failures[-1]["will_retry"] is False
    assert all(info["status_code"] == status and info["stage"] == "http_status" for info in failures)
    assert llm_env == ["acquire", "release"] * expected_attempts
    assert SECRET not in json.dumps(events) and BODY not in json.dumps(events)


@pytest.mark.asyncio
async def test_invalid_json_has_parse_stage_and_no_body_leak(llm_env, monkeypatch):
    async def handler(request):
        return response(request, content={"choices": [{"message": {"content": BODY + SECRET}}]})
    events = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(HttpClientProvider, "get_client", lambda: client)
        with pytest.raises(RuntimeError) as caught:
            await LLMClient.chat_json("s", "u", max_retries=0,
                on_event=lambda e, i: events.append((e, i)))
    assert get_llm_diagnostic(caught.value)["stage"] == "parse_response"
    assert SECRET not in str(caught.value) and BODY not in str(caught.value)


@pytest.mark.asyncio
async def test_callback_failure_does_not_mask_result(llm_env, monkeypatch, caplog):
    def bad_callback(event, info):
        raise ValueError(SECRET)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response(request))) as client:
        monkeypatch.setattr(HttpClientProvider, "get_client", lambda: client)
        assert await LLMClient.chat_json("s", "u", on_event=bad_callback) == {"answer": True}
    assert SECRET not in caplog.text
    assert llm_env == ["acquire", "release"]


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["slot_wait", "request"])
async def test_cancel_never_becomes_failure_or_retry(llm_env, monkeypatch, where):
    if where == "slot_wait":
        async def acquire():
            raise asyncio.CancelledError()
        monkeypatch.setattr(GlobalRateLimiter, "acquire", acquire)
    async def handler(request):
        raise asyncio.CancelledError()
    events = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(HttpClientProvider, "get_client", lambda: client)
        with pytest.raises(asyncio.CancelledError):
            await LLMClient.chat_json("s", "u", on_event=lambda e, i: events.append((e, i)))
    assert not {"request_failed", "retry_wait"}.intersection(e for e, _ in events)
    assert llm_env == ([] if where == "slot_wait" else ["acquire", "release"])


def test_safe_details_rejects_transport_payload_and_non_finite_numbers():
    assert safe_call_details({"headers": SECRET, "body": BODY, "elapsed_ms": float("inf"),
        "attempt": True, "trace_event": "x\\n" + SECRET, "stage": BODY}) == {}
