import pytest
from unittest.mock import MagicMock
import httpx
from app.services.retrieval.embedding import EmbeddingService
from app.services.http_client import HttpClientProvider
from app.config import settings

@pytest.mark.asyncio
async def test_embedding_remote_failure_raises_and_never_fakes_hash(monkeypatch):
    monkeypatch.setattr(settings, "EMBEDDING_MODE", "remote")
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "test-key")
    monkeypatch.setattr(settings, "EMBEDDING_BASE_URL", "https://api.example.com/v1")

    # Mock post to fail with 500
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError("500", request=MagicMock(), response=mock_resp)

    client = HttpClientProvider.get_client()
    async def mock_post(*args, **kwargs):
        raise httpx.HTTPStatusError("500", request=MagicMock(), response=mock_resp)

    monkeypatch.setattr(client, "post", mock_post)

    # Must raise RuntimeError and NOT return fake local vectors
    with pytest.raises(RuntimeError) as exc:
        await EmbeddingService.get_embedding("测试文本")
    assert "远程向量服务请求失败" in str(exc.value)

@pytest.mark.asyncio
async def test_embedding_verify_remote_compatibility(monkeypatch):
    monkeypatch.setattr(settings, "EMBEDDING_MODE", "remote")
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "test-key")
    monkeypatch.setattr(settings, "EMBEDDING_BASE_URL", "https://api.example.com/v1")

    client = HttpClientProvider.get_client()
    # Mock probe call
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "data": [{"embedding": [0.1] * 768, "index": 0}]
    }

    async def mock_post(*args, **kwargs):
        return mock_resp

    monkeypatch.setattr(client, "post", mock_post)

    res = await EmbeddingService.verify_remote_compatibility()
    assert isinstance(res, dict)
    assert res["mode"] == "remote"
    assert res["supports_batch"] is True
    assert res["dimension"] == 768

@pytest.mark.asyncio
async def test_embedding_batch_and_cache(monkeypatch):
    monkeypatch.setattr(settings, "EMBEDDING_MODE", "remote")
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "test-key")
    monkeypatch.setattr(settings, "EMBEDDING_BASE_URL", "https://api.example.com/v1")

    client = HttpClientProvider.get_client()
    call_count = 0

    async def mock_post(url, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        json_data = kwargs.get("json") or {}
        inputs = json_data.get("input", [])
        if isinstance(inputs, str):
            inputs = [inputs]
        data = [{"embedding": [0.05 * i] * 10, "index": i} for i in range(len(inputs))]
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"data": data}
        return resp

    monkeypatch.setattr(client, "post", mock_post)

    # 20 distinct texts -> should be grouped into batches of 16 + 4 = 2 requests
    texts = [f"unique text {i}" for i in range(20)]
    vectors = await EmbeddingService.get_embeddings_batch(texts)
    assert len(vectors) == 20
    assert call_count == 2

    # Querying the same texts again should hit cache (0 additional network calls)
    call_count_before = call_count
    vectors_cached = await EmbeddingService.get_embeddings_batch(texts)
    assert len(vectors_cached) == 20
    assert call_count == call_count_before
