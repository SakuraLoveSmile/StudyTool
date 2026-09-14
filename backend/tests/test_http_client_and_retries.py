import pytest
import httpx
from unittest.mock import MagicMock
from app.services.http_client import HttpClientProvider, GlobalRateLimiter
from app.services.llm.client import LLMClient
from app.config import settings

@pytest.mark.asyncio
async def test_http_client_provider_singleton():
    client1 = HttpClientProvider.get_client()
    client2 = HttpClientProvider.get_client()
    assert client1 is client2
    assert isinstance(client1, httpx.AsyncClient)

@pytest.mark.asyncio
async def test_global_rate_limiter():
    await GlobalRateLimiter.acquire()
    assert GlobalRateLimiter._semaphore._value == GlobalRateLimiter._max_concurrency - 1
    GlobalRateLimiter.release()
    assert GlobalRateLimiter._semaphore._value == GlobalRateLimiter._max_concurrency

    # Test 429 report
    await GlobalRateLimiter.report_429(backoff_seconds=0.01)
    assert GlobalRateLimiter._backoff_until > 0

@pytest.mark.asyncio
async def test_llm_fast_fail_on_client_errors(monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    monkeypatch.setattr(settings, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(settings, "LLM_BASE_URL", "https://api.example.com/v1")

    # Mock client to return 401 Unauthorized
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.text = "Unauthorized"
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError("Unauthorized", request=MagicMock(), response=mock_resp)

    call_count = 0
    async def mock_post(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return mock_resp

    client = HttpClientProvider.get_client()
    monkeypatch.setattr(client, "post", mock_post)

    with pytest.raises(RuntimeError) as exc:
        await LLMClient.chat_json("sys", "user")
    assert "401" in str(exc.value) or "未授权" in str(exc.value)
    # Must fast-fail without retrying
    assert call_count == 1

@pytest.mark.asyncio
async def test_llm_retry_on_429_with_callback(monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    monkeypatch.setattr(settings, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(settings, "LLM_BASE_URL", "https://api.example.com/v1")

    # First call returns 429, second succeeds
    resp_429 = MagicMock()
    resp_429.status_code = 429
    resp_429.text = "Rate limited"
    resp_429.headers = {"Retry-After": "0.01"}
    resp_429.raise_for_status.side_effect = httpx.HTTPStatusError("Rate limited", request=MagicMock(), response=resp_429)

    resp_200 = MagicMock()
    resp_200.status_code = 200
    resp_200.json.return_value = {
        "choices": [{"message": {"content": "{\"result\": \"success\"}"}}]
    }

    call_count = 0
    async def mock_post(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return resp_429
        return resp_200

    client = HttpClientProvider.get_client()
    monkeypatch.setattr(client, "post", mock_post)

    retry_callbacks = []
    def on_retry(attempt, delay, reason):
        retry_callbacks.append((attempt, delay, reason))

    res = await LLMClient.chat_json("sys", "user", max_retries=2, on_retry=on_retry)
    assert res == {"result": "success"}
    assert call_count == 2
    assert len(retry_callbacks) == 1
    assert retry_callbacks[0][0] == 1
