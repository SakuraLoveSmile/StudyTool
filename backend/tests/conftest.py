import pytest
import os
from pathlib import Path
from app.config import settings
from app.database import init_db


@pytest.fixture(scope="session", autouse=True)
def isolate_test_proxy_environment():
    # HTTP transport tests install their own transports. Host proxy settings
    # (including optional SOCKS transports) must not affect offline regression.
    saved = {key: value for key, value in os.environ.items() if key.lower().endswith("_proxy")}
    for key in saved:
        os.environ.pop(key, None)
    try:
        yield
    finally:
        os.environ.update(saved)

@pytest.fixture(autouse=True)
def isolate_test_environment(tmp_path, monkeypatch):
    test_data_dir = tmp_path / "test_data"
    test_data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "DATA_DIR", str(test_data_dir))
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    # Unit/API tests must not inherit paid remote providers from a developer .env.
    # Provider-specific tests explicitly install their own fake transport/config.
    monkeypatch.setattr(settings, "EMBEDDING_MODE", "mock")
    monkeypatch.setattr(settings, "OCR_MODE", "mock")
    monkeypatch.setattr(settings, "LLM_API_KEY", "")
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "")
    monkeypatch.setattr(settings, "OCR_API_KEY", "")
    monkeypatch.setattr(settings, "OCR_BASE_URL", "")
    monkeypatch.setattr(settings, "OCR_VISION_MODEL", "gpt-4o-mini")
    monkeypatch.setattr(settings, "BAIDU_OCR_TOKEN", "")
    init_db()
    yield
