"""Web status exposes server configuration metadata without service credentials."""
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


def test_web_status_redacts_service_credentials(monkeypatch):
    monkeypatch.setattr(settings, "DESKTOP_MODE", False)
    monkeypatch.setattr(settings, "PUBLIC_MODE", False)
    monkeypatch.setattr(settings, "OCR_MODE", "vision_llm")
    monkeypatch.setattr(settings, "OCR_BASE_URL", "https://ocr.example/v1?token=private-query")
    monkeypatch.setattr(settings, "OCR_API_KEY", "private-generic-ocr-key")
    monkeypatch.setattr(settings, "OCR_VISION_MODEL", "PaddlePaddle/PaddleOCR-VL-1.5")
    monkeypatch.setattr(settings, "LLM_API_KEY", "private-llm-key")
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "private-embedding-key")
    monkeypatch.setattr(settings, "BAIDU_OCR_TOKEN", "private-ocr-token")
    monkeypatch.setattr(settings, "LLM_BASE_URL", "https://user:secret@llm.example/v1?token=private-query#secret")
    monkeypatch.setattr(settings, "EMBEDDING_BASE_URL", "https://embedding.example/v1?key=private-query")
    monkeypatch.setattr(settings, "BAIDU_OCR_API_URL", "https://ocr.example/api/v2/ocr/jobs?token=private-query")
    response = TestClient(app).get("/api/desktop/status")
    assert response.status_code == 200
    data = response.json()
    assert data["llm_configured"] is True
    assert data["embedding_configured"] is True
    assert data["baidu_ocr_configured"] is True
    assert data["llm_base_url"] == "https://llm.example/v1"
    assert data["embedding_base_url"] == "https://embedding.example/v1"
    assert data["baidu_ocr_api_url"] == "https://ocr.example/api/v2/ocr/jobs"
    assert data["ocr"]["mode"] == "vision_llm"
    assert data["ocr"]["base_url"] == "https://ocr.example/v1"
    assert data["ocr"]["model"] == "PaddlePaddle/PaddleOCR-VL-1.5"
    for value in ("private-llm-key", "private-embedding-key", "private-ocr-token", "private-generic-ocr-key", "private-query", "secret", "user:"):
        assert value not in response.text
