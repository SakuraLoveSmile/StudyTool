"""Protocol contract tests for the Baidu PP-OCRv6 asynchronous jobs API adapter.

The wire contract fixed here follows the official PaddleOCR API SDK 3.7.0
(``paddleocr/_api_client/_http.py``, ``_core.py``, ``_poller.py``):

* ``POST {jobs_url}``  multipart ``model`` / ``optionalPayload`` / ``file`` + ``Bearer`` token
* ``GET  {jobs_url}/{jobId}``                                  + ``Bearer`` token
* ``GET  {resultUrl.jsonUrl}``  pre-signed object storage URL, **without** the token
* JSONL line shape: ``{"result": {"ocrResults": [{"prunedResult": {"rec_texts": [...]}}]}}``
"""

import asyncio
import contextlib
import inspect
import json
import logging
import re

import httpx
import pytest

import app.services.parser.ocr_service as ocr_mod
from app.config import settings
from app.services.http_client import GlobalRateLimiter, HttpClientProvider
from app.services.parser.ocr_service import (
    OCRQuotaExhaustedError,
    OCRService,
    OCRServiceError,
)

JOBS_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
JOB_ID = "8f3c1d2e4b5a6c7d"
JSONL_URL = "https://ocr-result.example.com/out/result.jsonl?X-Amz-Signature=deadbeefcafe"
TOKEN = "mock-token-xyz"
SIGNATURE = "deadbeefcafe"


# --------------------------------------------------------------------------- helpers

def _configure(monkeypatch, url=JOBS_URL, token=TOKEN):
    monkeypatch.setattr(settings, "OCR_MODE", "paddleocr_api")
    monkeypatch.setattr(settings, "BAIDU_OCR_API_URL", url)
    monkeypatch.setattr(settings, "BAIDU_OCR_TOKEN", token)


def _jsonl(*lines) -> str:
    """Builds JSONL text; each line is a list of per-page recognized-text lists."""
    out = []
    for pages in lines:
        out.append(json.dumps(
            {"result": {"ocrResults": [
                {"prunedResult": {"rec_texts": list(page)}} for page in pages
            ]}},
            ensure_ascii=False,
        ))
    return "\n".join(out)


def _done_response(json_url=JSONL_URL):
    return httpx.Response(200, json={"code": 0, "data": {
        "state": "done", "resultUrl": {"jsonUrl": json_url},
    }})


def _state_response(state, **extra):
    payload = {"state": state}
    payload.update(extra)
    return httpx.Response(200, json={"code": 0, "data": payload})


def _make_handler(*, submit=None, poll=None, download=None):
    """Routes jobs-API calls; every override receives the httpx request."""
    submit = submit or (lambda r: httpx.Response(200, json={"code": 0, "data": {"jobId": JOB_ID}}))
    poll = poll or (lambda r: _done_response())
    download = download or (lambda r: httpx.Response(200, text=_jsonl([["第一页文字", "1.1 考点"]])))

    def handler(request):
        if request.method == "POST":
            return submit(request)
        url = str(request.url)
        if url == JSONL_URL:
            return download(request)
        if url.startswith(f"{JOBS_URL}/"):
            return poll(request)
        raise AssertionError(f"unexpected request: {request.method} {url}")

    return handler


@contextlib.contextmanager
def _fast_sleep(monkeypatch, log=None):
    """Replaces asyncio.sleep so poll/retry waits cost no wall-clock time."""
    delays = []

    async def _sleep(delay):
        delays.append(delay)
        if log is not None:
            log.append(f"sleep:{delay}")

    monkeypatch.setattr(ocr_mod.asyncio, "sleep", _sleep)
    yield delays


@contextlib.contextmanager
def _patched_client(monkeypatch, handler):
    """Yields the list of requests seen by a MockTransport-backed shared client."""
    requests = []

    def recording(request):
        requests.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(recording))
    monkeypatch.setattr(HttpClientProvider, "get_client", lambda: client)
    yield requests


class _FakeClock:
    """Controllable clock injected as ``ocr_mod.time`` (only monotonic() is used)."""

    def __init__(self, start=0.0):
        self.now = start

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _poll_counter(states):
    """Returns a poll handler walking the given state script (last state repeats)."""
    index = {"n": 0}

    def poll(request):
        state = states[min(index["n"], len(states) - 1)]
        index["n"] += 1
        if state == "done":
            return _done_response()
        if state == "failed":
            return _state_response("failed", errorMsg="模型执行失败")
        return _state_response(state)

    return poll


# ----------------------------------------------------------------- submit contract

@pytest.mark.asyncio
async def test_submit_multipart_contract_and_full_flow(monkeypatch):
    """Jobs submit → 5s poll → token-less JSONL download, per acceptance criteria."""
    _configure(monkeypatch)
    events = []

    def on_event(event, info):
        events.append((event, info))

    with _fast_sleep(monkeypatch) as delays:
        with _patched_client(monkeypatch, _make_handler()) as requests:
            text = await OCRService.extract_text_from_image_bytes(
                b"\x89PNG-image-bytes", "png", on_event=on_event
            )

    assert text == "第一页文字\n1.1 考点"

    submit = requests[0]
    assert submit.method == "POST"
    assert str(submit.url) == JOBS_URL  # complete jobs address, nothing appended
    assert submit.headers["Authorization"] == f"Bearer {TOKEN}"
    assert submit.headers["Content-Type"].startswith("multipart/form-data")

    body = submit.read()
    assert re.search(rb'name="model"\r\n\r\nPP-OCRv6\r\n', body)
    assert re.search(rb'name="file"; filename="page\.png"', body)
    assert b"Content-Type: image/png" in body
    assert b"\x89PNG-image-bytes" in body  # raw binary image, not base64
    assert b"base64" not in body

    optional_payload = re.search(rb'name="optionalPayload"\r\n\r\n(.*?)\r\n', body, re.S)
    assert optional_payload, body[:400]
    decoded = json.loads(optional_payload.group(1).decode("utf-8"))
    assert decoded == {
        "useDocOrientationClassify": False,
        "useDocUnwarping": False,
        "useTextlineOrientation": False,
        "visualize": False,
    }
    assert b"ocrImage" not in body and b"visualize\": true" not in body

    poll = requests[1]
    assert poll.method == "GET"
    assert str(poll.url) == f"{JOBS_URL}/{JOB_ID}"
    assert poll.headers["Authorization"] == f"Bearer {TOKEN}"

    download = requests[2]
    assert str(download.url) == JSONL_URL
    assert "Authorization" not in download.headers  # pre-signed URL carries no token

    assert delays == []  # state was done on the first poll: no 5s wait
    assert [event for event, _ in events] == [
        "slot_wait", "request_start", "response_received",
        "slot_wait", "request_start", "response_received",
        "slot_wait", "request_start", "response_received",
    ]
    assert all(info.get("service") == "ocr" for _, info in events)


@pytest.mark.asyncio
async def test_jsonl_flattens_every_page_in_order(monkeypatch):
    """result.ocrResults[].prunedResult.rec_texts is flattened across JSONL lines."""
    _configure(monkeypatch)
    body = _jsonl(
        [["第一页首行", "  第一页第二行  "], ["续页文字"]],
        [["第二行记录", ""]],
    )
    handler = _make_handler(download=lambda r: httpx.Response(200, text=body))

    with _fast_sleep(monkeypatch):
        with _patched_client(monkeypatch, handler):
            text = await OCRService.extract_text_from_image_bytes(b"img", "png")

    assert text == "第一页首行\n第一页第二行\n续页文字\n第二行记录"


@pytest.mark.asyncio
async def test_jsonl_ignores_extra_unrelated_fields(monkeypatch):
    """Only rec_texts is read; visualization image fields are never downloaded."""
    _configure(monkeypatch)
    line = json.dumps({"result": {
        "dataInfo": {"type": "image"},
        "ocrResults": [{
            "prunedResult": {"rec_texts": ["正文"]},
            "ocrImage": "https://bucket.example.com/visualize/1.jpg?sig=abc",
            "inputImage": "https://bucket.example.com/input/1.jpg?sig=abc",
        }],
    }}, ensure_ascii=False)
    handler = _make_handler(download=lambda r: httpx.Response(200, text=line))

    with _fast_sleep(monkeypatch):
        with _patched_client(monkeypatch, handler) as requests:
            text = await OCRService.extract_text_from_image_bytes(b"img", "png")

    assert text == "正文"
    assert len(requests) == 3  # submit + poll + jsonl only (no visualize download)


# --------------------------------------------------------------------- polling

@pytest.mark.asyncio
async def test_poll_waits_five_seconds_without_holding_rate_limiter_slot(monkeypatch):
    """The 5s poll wait happens between release() and the next acquire()."""
    _configure(monkeypatch)
    handler = _make_handler(poll=_poll_counter(["pending", "running", "done"]))

    order = []
    real_acquire = GlobalRateLimiter.acquire
    real_release = GlobalRateLimiter.release

    async def spy_acquire():
        order.append("acquire")
        await real_acquire()

    def spy_release():
        order.append("release")
        real_release()

    monkeypatch.setattr(GlobalRateLimiter, "acquire", spy_acquire)
    monkeypatch.setattr(GlobalRateLimiter, "release", spy_release)

    with _fast_sleep(monkeypatch, log=order) as delays:
        with _patched_client(monkeypatch, handler) as requests:
            text = await OCRService.extract_text_from_image_bytes(b"img", "png")

    assert text
    assert delays == [5.0, 5.0]  # exactly the documented poll interval
    assert len(requests) == 5  # submit + 3 polls + download
    assert order == [
        "acquire", "release",
        "acquire", "release", "sleep:5.0",
        "acquire", "release", "sleep:5.0",
        "acquire", "release",
        "acquire", "release",
    ]


@pytest.mark.asyncio
async def test_every_request_timeout_is_capped_at_sixty_seconds(monkeypatch):
    _configure(monkeypatch)
    timeouts = []
    real_stream = ocr_mod.stream_request_context

    @contextlib.asynccontextmanager
    async def spy_stream(client, method, url, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        async with real_stream(client, method, url, **kwargs) as response:
            yield response

    monkeypatch.setattr(ocr_mod, "stream_request_context", spy_stream)

    with _fast_sleep(monkeypatch):
        with _patched_client(monkeypatch, _make_handler()):
            await OCRService.extract_text_from_image_bytes(b"img", "png")

    assert timeouts and all(0 < value <= 60.0 for value in timeouts)
    assert timeouts[0] == 60.0


@pytest.mark.asyncio
async def test_failed_and_unknown_states_raise_actionable_errors(monkeypatch):
    _configure(monkeypatch)

    with _fast_sleep(monkeypatch):
        with _patched_client(monkeypatch, _make_handler(poll=_poll_counter(["failed"]))) as requests:
            with pytest.raises(OCRServiceError) as exc:
                await OCRService.extract_text_from_image_bytes(b"img", "png")
    assert "任务执行失败" in str(exc.value)
    assert "模型执行失败" in str(exc.value)
    assert not any(r.method == "GET" and str(r.url) == JSONL_URL for r in requests)  # no download

    with _fast_sleep(monkeypatch):
        with _patched_client(monkeypatch, _make_handler(poll=lambda r: _state_response("queued"))):
            with pytest.raises(OCRServiceError) as exc:
                await OCRService.extract_text_from_image_bytes(b"img", "png")
    assert "状态异常" in str(exc.value)
    assert "queued" in str(exc.value)


@pytest.mark.asyncio
async def test_missing_job_id_raises(monkeypatch):
    _configure(monkeypatch)
    submit = lambda r: httpx.Response(200, json={"code": 0, "data": {"state": "pending"}})

    with _fast_sleep(monkeypatch):
        with _patched_client(monkeypatch, _make_handler(submit=submit)) as requests:
            with pytest.raises(OCRServiceError) as exc:
                await OCRService.extract_text_from_image_bytes(b"img", "png")

    assert "缺少 jobId" in str(exc.value)
    assert len(requests) == 1  # no polling without a job id


@pytest.mark.asyncio
async def test_missing_result_json_url_raises(monkeypatch):
    _configure(monkeypatch)
    for data in (
        {"state": "done"},
        {"state": "done", "resultUrl": {}},
        {"state": "done", "resultUrl": {"jsonUrl": "   "}},
    ):
        poll = (lambda payload: (lambda r: httpx.Response(200, json={"code": 0, "data": payload})))(data)
        with _fast_sleep(monkeypatch):
            with _patched_client(monkeypatch, _make_handler(poll=poll)):
                with pytest.raises(OCRServiceError) as exc:
                    await OCRService.extract_text_from_image_bytes(b"img", "png")
        assert "缺少 resultUrl.jsonUrl" in str(exc.value)


# -------------------------------------------------------------------- retries

@pytest.mark.asyncio
async def test_poll_retries_transient_5xx_twice_then_succeeds(monkeypatch):
    _configure(monkeypatch)
    attempts = {"poll": 0}

    def poll(request):
        attempts["poll"] += 1
        if attempts["poll"] < 3:
            return httpx.Response(500, text="Internal Server Error")
        return _done_response()

    events = []

    with _fast_sleep(monkeypatch) as delays:
        with _patched_client(monkeypatch, _make_handler(poll=poll)) as requests:
            text = await OCRService.extract_text_from_image_bytes(
                b"img", "png", on_event=lambda e, i: events.append((e, i))
            )

    assert text
    assert attempts["poll"] == 3
    assert delays == [1.0, 2.0]
    assert sum(1 for r in requests if r.method == "POST") == 1  # submit never repeated
    retries = [info for event, info in events if event == "retry_wait"]
    assert [info["wait_seconds"] for info in retries] == [1.0, 2.0]
    assert [info["attempt"] for info in retries] == [1, 2]


@pytest.mark.asyncio
async def test_poll_retry_limit_is_two(monkeypatch):
    _configure(monkeypatch)
    attempts = {"poll": 0}

    def poll(request):
        attempts["poll"] += 1
        return httpx.Response(503, text="Service Unavailable")

    with _fast_sleep(monkeypatch) as delays:
        with _patched_client(monkeypatch, _make_handler(poll=poll)):
            with pytest.raises(OCRServiceError) as exc:
                await OCRService.extract_text_from_image_bytes(b"img", "png")

    assert attempts["poll"] == 3  # initial + 2 retries
    assert delays == [1.0, 2.0]
    assert "重试耗尽" in str(exc.value) and "503" in str(exc.value)


@pytest.mark.asyncio
async def test_network_timeout_is_retried_like_a_transient_failure(monkeypatch):
    _configure(monkeypatch)
    attempts = {"poll": 0}

    def poll(request):
        attempts["poll"] += 1
        if attempts["poll"] == 1:
            raise httpx.ReadTimeout("read timed out", request=request)
        return _done_response()

    with _fast_sleep(monkeypatch) as delays:
        with _patched_client(monkeypatch, _make_handler(poll=poll)):
            text = await OCRService.extract_text_from_image_bytes(b"img", "png")

    assert text
    assert attempts["poll"] == 2
    assert delays == [1.0]


@pytest.mark.asyncio
async def test_download_retry_limit_is_two(monkeypatch):
    _configure(monkeypatch)
    attempts = {"download": 0}

    def download(request):
        attempts["download"] += 1
        return httpx.Response(502, text="Bad Gateway")

    with _fast_sleep(monkeypatch) as delays:
        with _patched_client(monkeypatch, _make_handler(download=download)):
            with pytest.raises(OCRServiceError) as exc:
                await OCRService.extract_text_from_image_bytes(b"img", "png")

    assert attempts["download"] == 3
    assert delays == [1.0, 2.0]
    assert "结果下载重试耗尽" in str(exc.value)


@pytest.mark.asyncio
async def test_submit_is_never_retried(monkeypatch):
    """A retried POST would create duplicate jobs, so submit fails immediately."""
    _configure(monkeypatch)
    events = []
    attempts = {"submit": 0}

    def submit(request):
        attempts["submit"] += 1
        return httpx.Response(500, text="Internal Server Error")

    with _fast_sleep(monkeypatch) as delays:
        with _patched_client(monkeypatch, _make_handler(submit=submit)) as requests:
            with pytest.raises(OCRServiceError) as exc:
                await OCRService.extract_text_from_image_bytes(
                    b"img", "png", on_event=lambda e, i: events.append((e, i))
                )

    assert attempts["submit"] == 1
    assert len(requests) == 1
    assert delays == []
    assert "提交失败" in str(exc.value)
    assert [event for event, _ in events] == ["slot_wait", "request_start", "response_received"]


@pytest.mark.asyncio
async def test_client_error_on_submit_fails_without_retry(monkeypatch):
    _configure(monkeypatch)
    attempts = {"submit": 0}

    def submit(request):
        attempts["submit"] += 1
        return httpx.Response(403, text="Invalid token")

    with _fast_sleep(monkeypatch):
        with _patched_client(monkeypatch, _make_handler(submit=submit)):
            with pytest.raises(OCRServiceError) as exc:
                await OCRService.extract_text_from_image_bytes(b"img", "png")

    assert attempts["submit"] == 1
    assert "HTTP 403" in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,payload", [
    (400, {"errorCode": "INVALID_TOKEN", "errorMsg": "凭证不可用"}),
    (200, {"code": 1001, "error": {"message": "凭证不可用"}}),
    (200, {"code": 1001, "data": {"errorMsg": "凭证不可用", "image": "private-image-output"}}),
])
async def test_submit_reports_safe_provider_error_without_retry(monkeypatch, caplog, status, payload):
    _configure(monkeypatch)
    caplog.set_level(logging.DEBUG)
    submit = lambda r: httpx.Response(status, json=payload, headers={"X-Request-Id": "ocr-request-123"})

    with _fast_sleep(monkeypatch) as delays:
        with _patched_client(monkeypatch, _make_handler(submit=submit)) as requests:
            with pytest.raises(OCRServiceError) as exc:
                await OCRService.extract_text_from_image_bytes(b"private-image-bytes", "png")

    assert str(exc.value).startswith(f"百度 OCR 任务提交失败（HTTP {status}）")
    assert "凭证不可用" in str(exc.value)
    assert "code=" in str(exc.value)
    assert len(requests) == 1 and requests[0].method == "POST"
    assert delays == []
    failures = [record for record in caplog.records if getattr(record, "event", None) == "request_failed"]
    assert len(failures) == 1
    record = failures[0]
    assert record.levelno == logging.WARNING
    assert record.status_code == status
    assert record.provider_request_id == "ocr-request-123"
    assert record.stage == record.failed_stage == "submit"
    assert record.call_id.startswith("ocr-baidu-job-")
    assert "private-image-bytes" not in caplog.text
    assert "private-image-output" not in str(exc.value) + caplog.text


@pytest.mark.asyncio
async def test_submit_redacts_exact_token_from_message_code_and_logs(monkeypatch, caplog):
    token = "plainOpaqueCredential_Z9"
    _configure(monkeypatch, token=token)
    caplog.set_level(logging.DEBUG)
    submit = lambda r: httpx.Response(400, json={
        "errorCode": f"invalid-{token}",
        "errorMsg": f"凭证 {token} 无效\n\x00请重新配置",
        "private": "do-not-echo-this-field",
    })

    with _patched_client(monkeypatch, _make_handler(submit=submit)) as requests:
        with pytest.raises(OCRServiceError) as exc:
            await OCRService.extract_text_from_image_bytes(b"private-image-bytes", "png")

    assert len(requests) == 1
    assert token not in str(exc.value)
    assert token not in caplog.text
    assert token not in str([record.__dict__ for record in caplog.records])
    assert "do-not-echo-this-field" not in str(exc.value) + caplog.text
    assert "[REDACTED]" in str(exc.value)
    assert "\n" not in str(exc.value) and "\x00" not in str(exc.value)


@pytest.mark.asyncio
async def test_submit_does_not_echo_non_json_error_body(monkeypatch, caplog):
    _configure(monkeypatch)
    caplog.set_level(logging.DEBUG)
    submit = lambda r: httpx.Response(400, text=f"<html>private error {TOKEN}</html>")

    with _patched_client(monkeypatch, _make_handler(submit=submit)) as requests:
        with pytest.raises(OCRServiceError) as exc:
            await OCRService.extract_text_from_image_bytes(b"img", "png")

    assert len(requests) == 1
    assert str(exc.value) == "百度 OCR 任务提交失败（HTTP 400）"
    assert "private error" not in caplog.text and TOKEN not in caplog.text


def test_submit_error_summary_is_bounded_and_only_reads_error_fields():
    summary = OCRService._jobs_safe_error_summary(json.dumps({
        "code": "c" * 100,
        "msg": "m" * 500,
        "data": {"image": "private-content"},
    }).encode(), "opaque-token")
    assert len(summary["reason_code"]) == 80
    assert len(summary["detail"]) == 300
    assert "private-content" not in str(summary)


@pytest.mark.asyncio
async def test_429_raises_quota_error_without_retry(monkeypatch):
    _configure(monkeypatch)

    submit_attempts = {"n": 0}

    def submit(request):
        submit_attempts["n"] += 1
        return httpx.Response(429, json={"msg": "Daily quota exceeded"})

    with _fast_sleep(monkeypatch):
        with _patched_client(monkeypatch, _make_handler(submit=submit)):
            with pytest.raises(OCRQuotaExhaustedError) as exc:
                await OCRService.extract_text_from_image_bytes(b"img", "png")
    assert submit_attempts["n"] == 1
    assert "429" in str(exc.value)

    poll_attempts = {"n": 0}

    def poll(request):
        poll_attempts["n"] += 1
        return httpx.Response(429, json={"msg": "rate limited"})

    with _fast_sleep(monkeypatch) as delays:
        with _patched_client(monkeypatch, _make_handler(poll=poll)):
            with pytest.raises(OCRQuotaExhaustedError):
                await OCRService.extract_text_from_image_bytes(b"img", "png")

    assert poll_attempts["n"] == 1
    assert delays == []


# -------------------------------------------------------------- result parsing

@pytest.mark.asyncio
async def test_malformed_jsonl_raises(monkeypatch):
    _configure(monkeypatch)
    handler = _make_handler(download=lambda r: httpx.Response(200, text='{"result": {"ocrResu'))

    with _fast_sleep(monkeypatch):
        with _patched_client(monkeypatch, handler):
            with pytest.raises(OCRServiceError) as exc:
                await OCRService.extract_text_from_image_bytes(b"img", "png")

    assert "JSONL" in str(exc.value)


@pytest.mark.asyncio
async def test_jsonl_missing_contract_fields_raises(monkeypatch):
    _configure(monkeypatch)
    for text in (
        '{"data": {}}',                                     # no result object
        '{"result": {"dataInfo": {}}}',                     # no ocrResults array
        '{"result": {"ocrResults": [{}]}}',                 # no prunedResult
        '{"result": {"ocrResults": [{"prunedResult": {}}]}}',  # no rec_texts
        '{"result": {"ocrResults": [{"prunedResult": {"rec_texts": [123]}}]}}',
        "[1, 2, 3]",                                        # JSON line that is not an object
    ):
        handler = _make_handler(download=lambda r, t=text: httpx.Response(200, text=t))
        with _fast_sleep(monkeypatch):
            with _patched_client(monkeypatch, handler):
                with pytest.raises(OCRServiceError):
                    await OCRService.extract_text_from_image_bytes(b"img", "png")


@pytest.mark.asyncio
async def test_empty_recognized_text_raises(monkeypatch):
    _configure(monkeypatch)
    for text in ("", "\n\n", _jsonl([[]]), _jsonl([["   ", ""]])):
        handler = _make_handler(download=lambda r, t=text: httpx.Response(200, text=t))
        with _fast_sleep(monkeypatch):
            with _patched_client(monkeypatch, handler):
                with pytest.raises(OCRServiceError) as exc:
                    await OCRService.extract_text_from_image_bytes(b"img", "png")
        assert "识别文本为空" in str(exc.value)


# ------------------------------------------------------------------ time limits

@pytest.mark.asyncio
async def test_whole_page_flow_hard_timeout_raises(monkeypatch):
    """A page that never finishes is abandoned past JOB_TOTAL_TIMEOUT (300s by default)."""
    _configure(monkeypatch)
    monkeypatch.setattr(OCRService, "JOB_TOTAL_TIMEOUT", 12.0)
    clock = _FakeClock()
    monkeypatch.setattr(ocr_mod, "time", clock)

    delays = []

    async def _sleep(delay):
        delays.append(delay)
        clock.advance(delay)

    monkeypatch.setattr(ocr_mod.asyncio, "sleep", _sleep)

    with _patched_client(monkeypatch, _make_handler(poll=lambda r: _state_response("running"))) as requests:
        with pytest.raises(OCRServiceError) as exc:
            await OCRService.extract_text_from_image_bytes(b"img", "png")

    assert "超时" in str(exc.value)
    # Poll interval 5s, then capped by the remaining budget: 5s + 5s + 2s = 12s.
    assert delays == [5.0, 5.0, 2.0]
    assert len(requests) == 4  # submit + 3 polls; the 4th poll is never sent
    assert OCRService.JOB_TOTAL_TIMEOUT == 12.0


# ------------------------------------------------------------------ cancellation

class _FakeResponse:
    def __init__(self):
        self.status_code = 200
        self.headers = {}
        self.closed = False

    async def aread(self):
        raise asyncio.CancelledError()

    async def aclose(self):
        self.closed = True


class _FakeCancellingClient:
    def __init__(self, response):
        self.response = response

    async def post(self, url, **kwargs):
        return self.response


@pytest.mark.asyncio
async def test_cancellation_releases_response_and_limiter_slot(monkeypatch):
    _configure(monkeypatch)
    GlobalRateLimiter._init()
    slot_before = GlobalRateLimiter._semaphore._value

    response = _FakeResponse()
    monkeypatch.setattr(HttpClientProvider, "get_client", lambda: _FakeCancellingClient(response))

    with pytest.raises(asyncio.CancelledError):
        await OCRService.extract_text_from_image_bytes(b"img", "png")

    assert response.closed is True  # opened response closed on cancellation
    assert GlobalRateLimiter._semaphore._value == slot_before  # slot released, not leaked


# ------------------------------------------------------- leakage & public surface

@pytest.mark.asyncio
async def test_token_signed_url_and_text_are_never_logged(monkeypatch, caplog):
    _configure(monkeypatch, token="tok-super-secret-42")
    caplog.set_level(logging.DEBUG, logger="app.diagnostics")

    with _fast_sleep(monkeypatch):
        with _patched_client(monkeypatch, _make_handler()):
            text = await OCRService.extract_text_from_image_bytes(b"img", "png")
    assert text == "第一页文字\n1.1 考点"

    def download(request):
        return httpx.Response(502, text="Bad Gateway")

    with _fast_sleep(monkeypatch):
        with _patched_client(monkeypatch, _make_handler(download=download)):
            with pytest.raises(OCRServiceError) as exc:
                await OCRService.extract_text_from_image_bytes(b"img", "png")
    failure = exc.value

    app_records = [r for r in caplog.records if r.name.startswith("app.")]
    blob = "\n".join(
        str(part)
        for record in app_records
        for part in (
            record.getMessage(),
            getattr(record, "endpoint", ""),
            getattr(record, "retry_reason", ""),
            getattr(record, "exception_type", ""),
        )
    )

    for secret in ("tok-super-secret-42", SIGNATURE, "X-Amz-Signature", "第一页文字", JSONL_URL):
        assert secret not in blob, f"{secret} leaked into diagnostics"
    assert JOB_ID not in blob  # job id is templated by sanitize_endpoint
    assert "{id}" in blob or "jobs" in blob

    assert "tok-super-secret-42" not in str(failure)
    assert SIGNATURE not in str(failure)


def test_public_api_surface_is_unchanged():
    signature = inspect.signature(OCRService.extract_text_from_image_bytes)
    assert list(signature.parameters) == ["image_bytes", "image_format", "on_event", "max_attempts"]
    for name in ("image_bytes", "image_format", "on_event"):
        assert signature.parameters[name].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert signature.parameters["max_attempts"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["max_attempts"].default == 3
    assert signature.parameters["image_format"].default == "png"
    assert signature.parameters["on_event"].default is None
    assert inspect.iscoroutinefunction(OCRService.extract_text_from_image_bytes)

    override_signature = inspect.signature(OCRService._extract_via_paddleocr_api)
    assert list(override_signature.parameters) == [
        "image_bytes", "image_format", "on_event", "override_api_url", "override_token",
    ]
    assert override_signature.parameters["override_api_url"].default is None
    assert override_signature.parameters["override_token"].default is None

    assert issubclass(OCRQuotaExhaustedError, OCRServiceError)


@pytest.mark.asyncio
async def test_override_url_and_token_win_over_settings(monkeypatch):
    _configure(monkeypatch, url="https://stale.example.com/api/v1/ocr", token="stale-token")

    with _fast_sleep(monkeypatch):
        with _patched_client(monkeypatch, _make_handler()) as requests:
            text = await OCRService._extract_via_paddleocr_api(
                b"img", "png", None,
                override_api_url=JOBS_URL, override_token="override-token",
            )

    assert text == "第一页文字\n1.1 考点"
    assert str(requests[0].url) == JOBS_URL
    assert requests[0].headers["Authorization"] == "Bearer override-token"
    assert all("stale.example.com" not in str(r.url) for r in requests)


@pytest.mark.asyncio
async def test_missing_configuration_fails_before_any_request(monkeypatch):
    monkeypatch.setattr(settings, "BAIDU_OCR_API_URL", "")
    monkeypatch.setattr(settings, "BAIDU_OCR_TOKEN", "")

    with pytest.raises(OCRServiceError) as exc:
        await OCRService._extract_via_paddleocr_api(b"img", "png")
    assert "未配置" in str(exc.value)


@pytest.mark.asyncio
async def test_submit_rejection_and_non_json_body_raise(monkeypatch):
    _configure(monkeypatch)

    rejected = lambda r: httpx.Response(200, json={"code": 1001, "msg": "参数非法"})
    with _fast_sleep(monkeypatch):
        with _patched_client(monkeypatch, _make_handler(submit=rejected)):
            with pytest.raises(OCRServiceError) as exc:
                await OCRService.extract_text_from_image_bytes(b"img", "png")
    assert "code=1001" in str(exc.value)
    assert "参数非法" in str(exc.value)  # only the sanitized error field is summarized

    html = lambda r: httpx.Response(200, text="<html>gateway</html>")
    with _fast_sleep(monkeypatch):
        with _patched_client(monkeypatch, _make_handler(submit=html)):
            with pytest.raises(OCRServiceError) as exc:
                await OCRService.extract_text_from_image_bytes(b"img", "png")
    assert "JSON" in str(exc.value)
