"""Offline contracts for independent OpenAI-compatible vision OCR providers."""

import asyncio
import base64
import json
import logging
from unittest.mock import AsyncMock, Mock

import fitz
import httpx
import pytest

import app.services.parser.ocr_service as ocr_module
from app.config import settings
from app.services.http_client import GlobalRateLimiter, HttpClientProvider
from app.services.parser.ocr_service import OCRService, OCRServiceError


OCR_URL = "https://api.siliconflow.cn/v1"
OCR_MODEL = "PaddlePaddle/PaddleOCR-VL-1.5"
OCR_KEY = "opaqueVisionCredential_A9"
LLM_KEY = "differentSharedCredential_B7"


@pytest.fixture(autouse=True)
def generic_configuration(monkeypatch):
    monkeypatch.setattr(settings, "OCR_MODE", "vision_llm")
    monkeypatch.setattr(settings, "OCR_BASE_URL", OCR_URL)
    monkeypatch.setattr(settings, "OCR_API_KEY", OCR_KEY)
    monkeypatch.setattr(settings, "OCR_VISION_MODEL", OCR_MODEL)
    monkeypatch.setattr(settings, "OCR_TIMEOUT", 120)
    monkeypatch.setattr(settings, "LLM_BASE_URL", "https://other-llm.example/v1")
    monkeypatch.setattr(settings, "LLM_API_KEY", LLM_KEY)
    monkeypatch.setattr(settings, "LLM_MODEL", "different-chat-model")
    monkeypatch.setattr(settings, "LLM_TIMEOUT", 7)
    monkeypatch.setattr(settings, "BAIDU_OCR_TOKEN", "baidu-private-token")
    monkeypatch.setattr(settings, "BAIDU_OCR_API_URL", "https://baidu.example/jobs")


@pytest.fixture
def png_bytes():
    doc = fitz.open()
    page = doc.new_page(width=200, height=80)
    page.insert_text((10, 40), "OCR_TEST_2026")
    data = page.get_pixmap().tobytes("png")
    doc.close()
    return data


def _response(content="识别文字", *, finish_reason="stop", refusal=None):
    return {"choices": [{"finish_reason": finish_reason, "message": {"content": content, "refusal": refusal}}]}


def _transport(monkeypatch, handler):
    requests = []

    def record(request):
        requests.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    monkeypatch.setattr(HttpClientProvider, "get_client", lambda: client)
    return client, requests


@pytest.mark.asyncio
async def test_dedicated_vision_contract_uses_own_endpoint_key_model_timeout_and_real_image(monkeypatch, png_bytes):
    events = []
    client, requests = _transport(monkeypatch, lambda request: httpx.Response(200, json=_response()))
    async with client:
        text = await OCRService.extract_text_from_image_bytes(
            png_bytes, "png", on_event=lambda event, info: events.append(event),
        )

    assert text == "识别文字"
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == f"{OCR_URL}/chat/completions"
    assert request.headers["authorization"] == f"Bearer {OCR_KEY}"
    assert request.extensions["timeout"]["read"] == 120
    payload = json.loads(request.content)
    assert payload["model"] == OCR_MODEL
    assert payload["messages"][0]["content"][0] == {"type": "text", "text": "OCR:"}
    image_url = payload["messages"][0]["content"][1]["image_url"]["url"]
    prefix, encoded = image_url.split(",", 1)
    assert prefix == "data:image/png;base64"
    assert base64.b64decode(encoded) == png_bytes
    assert png_bytes.startswith(b"\x89PNG")
    assert LLM_KEY not in request.content.decode()
    assert events == ["slot_wait", "request_start", "response_received"]


@pytest.mark.asyncio
async def test_text_blocks_are_combined(monkeypatch, png_bytes):
    payload = _response([{"type": "text", "text": "第一行"}, {"type": "text", "text": "第二行"}])
    client, _ = _transport(monkeypatch, lambda request: httpx.Response(200, json=payload))
    async with client:
        assert await OCRService.extract_text_from_image_bytes(png_bytes) == "第一行\n第二行"


@pytest.mark.asyncio
async def test_other_vision_model_keeps_general_transcription_prompt_and_supports_http(monkeypatch, png_bytes):
    monkeypatch.setattr(settings, "OCR_BASE_URL", "http://local-ocr.example/v1/")
    monkeypatch.setattr(settings, "OCR_VISION_MODEL", "other-vision-model")
    client, requests = _transport(monkeypatch, lambda request: httpx.Response(200, json=_response()))
    async with client:
        await OCRService.extract_text_from_image_bytes(png_bytes)
    assert str(requests[0].url) == "http://local-ocr.example/v1/chat/completions"
    prompt = json.loads(requests[0].content)["messages"][0]["content"][0]["text"]
    assert "精确识别" in prompt and prompt != "OCR:"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401])
async def test_http_error_reason_is_safe_and_not_retried(monkeypatch, png_bytes, caplog, status):
    caplog.set_level(logging.DEBUG)
    payload = {"error": {
        "code": f"invalid-{OCR_KEY}", "message": f"Key {OCR_KEY} cannot access this model\n\x00",
    }, "image": "private-response-image"}
    client, requests = _transport(monkeypatch, lambda request: httpx.Response(status, json=payload))
    async with client:
        with pytest.raises(OCRServiceError) as exc:
            await OCRService.extract_text_from_image_bytes(png_bytes)
    message = str(exc.value)
    assert f"HTTP {status}" in message and "cannot access this model" in message
    assert "[REDACTED]" in message
    assert OCR_KEY not in message + caplog.text
    assert "private-response-image" not in message + caplog.text
    assert "\n" not in message and "\x00" not in message
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_http_non_json_error_does_not_echo_html(monkeypatch, png_bytes, caplog):
    caplog.set_level(logging.DEBUG)
    client, requests = _transport(monkeypatch, lambda request: httpx.Response(400, text=f"<html>{OCR_KEY}-private</html>"))
    async with client:
        with pytest.raises(OCRServiceError) as exc:
            await OCRService.extract_text_from_image_bytes(png_bytes)
    assert str(exc.value) == "OCR 请求失败（HTTP 400）"
    assert OCR_KEY not in caplog.text and "<html>" not in caplog.text
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,reason", [
    (_response(" "), "空文本"),
    (_response(None), "空文本"),
    (_response("incomplete page", finish_reason="length"), "截断"),
    (_response("some text", refusal="refused"), "拒绝"),
    (_response([{"type": "refusal", "refusal": "refused"}]), "拒绝"),
    ({"choices": []}, "choices"),
    ({"choices": [{"message": []}]}, "message"),
    ([], "choices"),
])
async def test_empty_truncated_refused_and_malformed_results_fail(monkeypatch, png_bytes, payload, reason):
    client, requests = _transport(monkeypatch, lambda request: httpx.Response(200, json=payload))
    async with client:
        with pytest.raises(OCRServiceError, match=reason):
            await OCRService.extract_text_from_image_bytes(png_bytes)
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_invalid_json_response_is_readable(monkeypatch, png_bytes):
    client, _ = _transport(monkeypatch, lambda request: httpx.Response(200, text="broken-json-private"))
    async with client:
        with pytest.raises(OCRServiceError, match="非 JSON 或畸形响应"):
            await OCRService.extract_text_from_image_bytes(png_bytes)


@pytest.mark.asyncio
@pytest.mark.parametrize("base,key", [(OCR_URL, ""), ("", OCR_KEY)])
async def test_partial_dedicated_config_never_borrows_shared_llm_credentials(monkeypatch, png_bytes, base, key):
    monkeypatch.setattr(settings, "OCR_BASE_URL", base)
    monkeypatch.setattr(settings, "OCR_API_KEY", key)
    get_client = Mock(side_effect=AssertionError("configuration must fail before networking"))
    monkeypatch.setattr(HttpClientProvider, "get_client", get_client)
    with pytest.raises(OCRServiceError, match="同时设置 OCR_BASE_URL 和 OCR_API_KEY"):
        await OCRService.extract_text_from_image_bytes(png_bytes)
    get_client.assert_not_called()
    assert OCRService.configuration_status()["source"] == "dedicated"
    assert OCRService.configuration_status()["configured"] is False


@pytest.mark.asyncio
async def test_shared_legacy_configuration_keeps_ocr_model(monkeypatch, png_bytes):
    monkeypatch.setattr(settings, "OCR_BASE_URL", "")
    monkeypatch.setattr(settings, "OCR_API_KEY", "")
    client, requests = _transport(monkeypatch, lambda request: httpx.Response(200, json=_response()))
    async with client:
        await OCRService.extract_text_from_image_bytes(png_bytes)
    assert str(requests[0].url) == "https://other-llm.example/v1/chat/completions"
    assert requests[0].headers["authorization"] == f"Bearer {LLM_KEY}"
    assert json.loads(requests[0].content)["model"] == OCR_MODEL
    assert OCRService.configuration_status()["source"] == "llm_shared"


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value,reason", [
    ("OCR_BASE_URL", "ftp://invalid.example/v1", "HTTP/HTTPS"),
    ("OCR_BASE_URL", "https://example.test:bad/v1", "HTTP/HTTPS"),
    ("OCR_BASE_URL", "https://user:password@example.test/v1", "HTTP/HTTPS"),
    ("OCR_BASE_URL", "https://example.test/v1?key=secret", "HTTP/HTTPS"),
    ("OCR_API_KEY", "not a valid key", "API Key"),
    ("OCR_API_KEY", "不是有效凭据", "API Key"),
    ("OCR_VISION_MODEL", "", "视觉模型"),
    ("OCR_VISION_MODEL", "invalid\nmodel", "视觉模型"),
    ("OCR_TIMEOUT", 0, "超时"),
])
async def test_invalid_configuration_fails_before_network(monkeypatch, png_bytes, field, value, reason):
    monkeypatch.setattr(settings, field, value)
    get_client = Mock(side_effect=AssertionError("configuration must fail before networking"))
    monkeypatch.setattr(HttpClientProvider, "get_client", get_client)
    with pytest.raises(OCRServiceError, match=reason):
        await OCRService.extract_text_from_image_bytes(png_bytes)
    get_client.assert_not_called()
    assert OCRService.configuration_status()["configured"] is False


def test_configuration_status_is_static_complete_and_secret_free(monkeypatch):
    get_client = Mock(side_effect=AssertionError("status must never network"))
    monkeypatch.setattr(HttpClientProvider, "get_client", get_client)
    status = OCRService.configuration_status()
    assert set(status) == {"mode", "provider", "base_url", "model", "configured", "message", "source"}
    assert status["provider"] == "OpenAI 兼容视觉接口"
    assert status["base_url"] == OCR_URL
    assert status["model"] == OCR_MODEL
    assert status["configured"] is True
    assert status["source"] == "dedicated"
    for secret in (OCR_KEY, LLM_KEY, "baidu-private-token"):
        assert secret not in json.dumps(status)
    monkeypatch.setattr(settings, "OCR_BASE_URL", f"https://user:private@example.test/v1?token={OCR_KEY}#secret")
    status = OCRService.configuration_status()
    assert status["base_url"] == "https://example.test/v1"
    assert status["configured"] is False
    assert "private" not in json.dumps(status) and OCR_KEY not in json.dumps(status)
    get_client.assert_not_called()


@pytest.mark.parametrize("mode,provider,configured", [
    ("paddleocr_api", "百度 PaddleOCR", True), ("mock", "", False), ("unknown", "", False),
])
def test_configuration_status_for_selected_provider(monkeypatch, mode, provider, configured):
    monkeypatch.setattr(settings, "OCR_MODE", mode)
    status = OCRService.configuration_status()
    assert status["provider"] == provider
    assert status["configured"] is configured
    assert status["message"]
    assert "baidu-private-token" not in json.dumps(status)


@pytest.mark.asyncio
async def test_probe_max_attempts_one_does_not_retry_or_wait(monkeypatch, png_bytes):
    sleep = AsyncMock()
    monkeypatch.setattr(ocr_module.asyncio, "sleep", sleep)
    client, requests = _transport(monkeypatch, lambda request: httpx.Response(503, json={"message": "temporarily unavailable"}))
    async with client:
        with pytest.raises(OCRServiceError, match="HTTP 503"):
            await OCRService.extract_text_from_image_bytes(png_bytes, max_attempts=1)
    assert len(requests) == 1
    sleep.assert_not_called()


@pytest.mark.asyncio
async def test_default_attempts_retain_transient_retry(monkeypatch, png_bytes):
    sleep = AsyncMock()
    monkeypatch.setattr(ocr_module.asyncio, "sleep", sleep)
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, json={"message": "unavailable"}) if attempts < 3 else httpx.Response(200, json=_response())

    client, requests = _transport(monkeypatch, handler)
    async with client:
        assert await OCRService.extract_text_from_image_bytes(png_bytes) == "识别文字"
    assert len(requests) == 3
    assert sleep.await_count == 2


@pytest.mark.asyncio
async def test_cancellation_propagates_and_releases_limiter(monkeypatch, png_bytes):
    acquire, release = AsyncMock(), Mock()
    monkeypatch.setattr(GlobalRateLimiter, "acquire", acquire)
    monkeypatch.setattr(GlobalRateLimiter, "release", release)

    def cancel(request):
        raise asyncio.CancelledError()

    client, requests = _transport(monkeypatch, cancel)
    async with client:
        with pytest.raises(asyncio.CancelledError):
            await OCRService.extract_text_from_image_bytes(png_bytes)
    assert len(requests) == 1
    acquire.assert_awaited_once()
    release.assert_called_once()
