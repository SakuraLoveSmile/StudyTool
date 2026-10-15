"""Selected-provider OCR probes use synthetic images and isolated HTTP transports."""
import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services.http_client import HttpClientProvider
from app.services.parser.ocr_service import OCRService


@pytest.fixture
def vision_config(monkeypatch):
    monkeypatch.setattr(settings, "DESKTOP_MODE", False)
    monkeypatch.setattr(settings, "PUBLIC_MODE", False)
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    monkeypatch.setattr(settings, "OCR_MODE", "vision_llm")
    monkeypatch.setattr(settings, "OCR_BASE_URL", "https://ocr.example/v1")
    monkeypatch.setattr(settings, "OCR_API_KEY", "private-ocr-probe-key")
    monkeypatch.setattr(settings, "OCR_VISION_MODEL", "PaddlePaddle/PaddleOCR-VL-1.5")


def _probe():
    return TestClient(app).post("/api/desktop/test-connection", json={"service_type": "ocr"})


@pytest.mark.parametrize("content,ok", [("TEST_OCR_PROBE_2026", True), ("我是一个聊天模型", False)])
def test_probe_requires_recognized_test_text(vision_config, monkeypatch, content, ok):
    requests = []

    def handle(request):
        requests.append(request)
        data = json.loads(request.content)
        assert str(request.url) == "https://ocr.example/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer private-ocr-probe-key"
        assert data["model"] == "PaddlePaddle/PaddleOCR-VL-1.5"
        parts = data["messages"][0]["content"]
        image = next(p["image_url"]["url"] for p in parts if p["type"] == "image_url")
        assert base64.b64decode(image.split(",", 1)[1]).startswith(b"\x89PNG\r\n")
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(HttpClientProvider, "get_client", lambda: client)
    response = _probe()
    assert response.status_code == 200
    assert response.json()["ok"] is ok
    assert len(requests) == 1


def test_probe_makes_only_one_attempt_on_transient_failure(vision_config, monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(503, json={"error": {"message": "service unavailable"}})

    monkeypatch.setattr(HttpClientProvider, "get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    response = _probe()
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "503" in response.json()["message"]
    assert len(requests) == 1


def test_probe_rejects_incomplete_dedicated_config_before_any_call(vision_config, monkeypatch):
    monkeypatch.setattr(settings, "OCR_API_KEY", "")
    monkeypatch.setattr(settings, "LLM_API_KEY", "do-not-send-main-key")

    def unexpected_client():
        raise AssertionError("Incomplete OCR configuration must never send the main LLM key")

    monkeypatch.setattr(HttpClientProvider, "get_client", unexpected_client)
    response = _probe()
    assert response.status_code == 400
    assert "do-not-send-main-key" not in response.text


def test_probe_dispatches_selected_baidu_provider(vision_config, monkeypatch):
    monkeypatch.setattr(settings, "OCR_MODE", "paddleocr_api")
    monkeypatch.setattr(settings, "BAIDU_OCR_API_URL", "https://ocr.example/api/v2/ocr/jobs")
    monkeypatch.setattr(settings, "BAIDU_OCR_TOKEN", "private-baidu-key")
    calls = []

    async def selected_baidu(cls, image_bytes, image_format, on_event=None):
        calls.append(image_format)
        return "TEST_OCR_PROBE_2026"

    monkeypatch.setattr(OCRService, "_extract_via_paddleocr_api", classmethod(selected_baidu))
    assert _probe().json()["ok"] is True
    assert calls == ["png"]
