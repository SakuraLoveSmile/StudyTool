import asyncio
import hashlib
import json
import logging
import time
from typing import List, Dict, Any, Optional
import httpx
import numpy as np

from app.config import settings
from app.database import EmbeddingSource, current_embedding_source
from app.services.http_client import HttpClientProvider, GlobalRateLimiter
from app.diagnostics import (
    call_context,
    log_diagnostic,
    sanitize_endpoint,
    extract_provider_request_id,
    stream_request_context,
    extract_safe_exception_info,
)

logger = logging.getLogger(__name__)


def _cache_tenant_id() -> str:
    """Return the authenticated tenant, refusing shared cache use in public mode."""
    try:
        from app.tenancy import current_tenant_id
        tenant_id = current_tenant_id()
    except (ImportError, AttributeError):
        tenant_id = None
    if tenant_id:
        return str(tenant_id)
    if bool(getattr(settings, "PUBLIC_MODE", False)):
        raise RuntimeError("Embedding cache requires an authenticated tenant context")
    return "legacy"

class EmbeddingService:
    DIMENSION = 256
    _compatibility_cache: Optional[Dict[str, Any]] = None
    _compatibility_source: Optional[EmbeddingSource] = None
    _vector_cache: Dict[str, np.ndarray] = {}
    _MAX_CACHE_ITEMS = 5000

    @classmethod
    def _cache_key(cls, text: str, source: Optional[EmbeddingSource] = None) -> str:
        source = source or current_embedding_source()
        tenant_id = _cache_tenant_id()
        identity = [tenant_id, source.mode, source.model, source.base_url, text]
        return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode("utf-8")).hexdigest()

    @staticmethod
    def _assert_source(source: EmbeddingSource) -> None:
        if current_embedding_source() != source:
            raise RuntimeError("向量配置在生成期间发生变化，请按当前配置重新建立索引")

    @classmethod
    async def verify_remote_compatibility(cls) -> Dict[str, Any]:
        """
        Validates API compatibility with minimal synthetic input.
        Tests whether provider supports array input or single string only.
        Fails fast if credentials, URL, or model are invalid.
        """
        source = current_embedding_source()
        if cls._compatibility_cache is not None and cls._compatibility_source == source:
            return cls._compatibility_cache

        if source.mode != "remote":
            cls._compatibility_source = source
            cls._compatibility_cache = {"mode": "mock", "supports_batch": True, "dimension": cls.DIMENSION}
            return cls._compatibility_cache

        if not settings.EMBEDDING_API_KEY:
            raise RuntimeError("Embedding 未配置 API 密钥；正式 remote 模式禁止退回伪向量")

        base_url = source.base_url
        endpoint = sanitize_endpoint(f"{base_url}/embeddings")
        client = HttpClientProvider.get_client()
        headers = {
            "Authorization": f"Bearer {settings.EMBEDDING_API_KEY}",
            "Content-Type": "application/json"
        }

        # Probe 1: Array input test with minimal synthetic token
        probe_array = {"input": ["ping"], "model": source.model}
        supports_batch = False
        detected_dim = cls.DIMENSION

        with call_context() as probe_cid:
            await GlobalRateLimiter.acquire()
            t_req = time.monotonic()
            current_stage = "request_send"
            try:
                log_diagnostic(
                    "request_dispatched",
                    level=logging.INFO,
                    message="Embedding 批量能力探测请求已发送",
                    service="embedding",
                    model=settings.EMBEDDING_MODEL,
                    endpoint=endpoint,
                    input_chars=4,
                    timeout_config=15.0
                )
                current_stage = "await_headers"
                async with stream_request_context(client, "POST", f"{base_url}/embeddings", json=probe_array, headers=headers, timeout=15.0) as res:
                    headers_ms = (time.monotonic() - t_req) * 1000.0
                    req_id = extract_provider_request_id(res.headers)
                    log_diagnostic(
                        "response_headers",
                        level=logging.DEBUG,
                        message="收到 Embedding 探测响应头",
                        service="embedding",
                        model=settings.EMBEDDING_MODEL,
                        status_code=res.status_code,
                        provider_request_id=req_id,
                        duration_ms=round(headers_ms, 2)
                    )
                    current_stage = "read_body"
                    t_body = time.monotonic()
                    body_bytes = await res.aread()
                    body_ms = (time.monotonic() - t_body) * 1000.0
                    log_diagnostic(
                        "response_body",
                        level=logging.DEBUG,
                        message="Embedding 探测响应体读取完成",
                        service="embedding",
                        model=settings.EMBEDDING_MODEL,
                        status_code=res.status_code,
                        provider_request_id=req_id,
                        duration_ms=round(body_ms, 2),
                        input_bytes=len(body_bytes)
                    )
                    if res.status_code == 200:
                        data = json.loads(body_bytes.decode("utf-8"))
                        emb = data["data"][0]["embedding"]
                        detected_dim = len(emb)
                        supports_batch = True
                        logger.info("Embedding provider verified: array batch supported, dimension=%d", detected_dim)
                    elif res.status_code in (401, 403):
                        raise RuntimeError(f"Embedding 服务鉴权失败 (HTTP {res.status_code})，请核对 EMBEDDING_API_KEY")
                    elif res.status_code == 404:
                        raise RuntimeError(f"Embedding 模型或端点不存在 (HTTP 404: {settings.EMBEDDING_MODEL})")
                    else:
                        logger.info("Embedding probe array returned HTTP %d, testing single string fallback...", res.status_code)
            except (RuntimeError, Exception) as exc:
                if isinstance(exc, RuntimeError):
                    raise
                logger.warning("Embedding array probe failed: %s. Trying single string...", type(exc).__name__)
            finally:
                GlobalRateLimiter.release()

            # Probe 2: If array failed or returned non-200, test single string input
            if not supports_batch:
                probe_str = {"input": "ping", "model": source.model}
                await GlobalRateLimiter.acquire()
                t_req = time.monotonic()
                current_stage = "request_send"
                try:
                    log_diagnostic(
                        "request_dispatched",
                        level=logging.INFO,
                        message="Embedding 单字符串探测请求已发送",
                        service="embedding",
                        model=settings.EMBEDDING_MODEL,
                        endpoint=endpoint,
                        input_chars=4,
                        timeout_config=15.0
                    )
                    current_stage = "await_headers"
                    async with stream_request_context(client, "POST", f"{base_url}/embeddings", json=probe_str, headers=headers, timeout=15.0) as res:
                        headers_ms = (time.monotonic() - t_req) * 1000.0
                        req_id = extract_provider_request_id(res.headers)
                        log_diagnostic(
                            "response_headers",
                            level=logging.DEBUG,
                            message="收到 Embedding 单字符串探测响应头",
                            service="embedding",
                            model=settings.EMBEDDING_MODEL,
                            status_code=res.status_code,
                            provider_request_id=req_id,
                            duration_ms=round(headers_ms, 2)
                        )
                        current_stage = "read_body"
                        t_body = time.monotonic()
                        body_bytes = await res.aread()
                        body_ms = (time.monotonic() - t_body) * 1000.0
                        log_diagnostic(
                            "response_body",
                            level=logging.DEBUG,
                            message="Embedding 单字符串探测响应体读取完成",
                            service="embedding",
                            model=settings.EMBEDDING_MODEL,
                            status_code=res.status_code,
                            provider_request_id=req_id,
                            duration_ms=round(body_ms, 2),
                            input_bytes=len(body_bytes)
                        )
                        if res.status_code == 200:
                            data = json.loads(body_bytes.decode("utf-8"))
                            emb = data["data"][0]["embedding"]
                            detected_dim = len(emb)
                            supports_batch = False
                            logger.info("Embedding provider verified: single-string only, dimension=%d", detected_dim)
                        elif res.status_code in (401, 403):
                            raise RuntimeError(f"Embedding 服务鉴权失败 (HTTP {res.status_code})")
                        elif res.status_code == 404:
                            raise RuntimeError(f"Embedding 模型或端点不存在 (HTTP 404: {settings.EMBEDDING_MODEL})")
                        else:
                            raise RuntimeError(f"Embedding 服务探测失败 (HTTP {res.status_code})")
                except httpx.HTTPStatusError as err:
                    raise RuntimeError(f"Embedding 服务请求失败 (HTTP {err.response.status_code})") from err
                except Exception as exc:
                    if isinstance(exc, RuntimeError):
                        raise
                    raise RuntimeError(f"Embedding 服务网络不可达: {type(exc).__name__}") from exc
                finally:
                    GlobalRateLimiter.release()

        cls._assert_source(source)
        cls._compatibility_source = source
        cls._compatibility_cache = {
            "mode": "remote",
            "supports_batch": supports_batch,
            "dimension": detected_dim
        }
        return cls._compatibility_cache

    @classmethod
    async def get_embedding(cls, text: str) -> np.ndarray:
        """Returns a normalized 1D float32 numpy array embedding."""
        if not text or not text.strip():
            return np.zeros(cls.DIMENSION, dtype=np.float32)

        source = current_embedding_source()
        cache_key = cls._cache_key(text, source)
        if cache_key in cls._vector_cache:
            return cls._vector_cache[cache_key]

        if source.mode == "remote":
            # STRICT: never silently fall back to mock hash!
            vec = await cls._get_remote_embedding(text, source=source)
        else:
            vec = cls._get_mock_embedding(text)

        cls._assert_source(source)
        if len(cls._vector_cache) < cls._MAX_CACHE_ITEMS:
            cls._vector_cache[cache_key] = vec
        return vec

    @classmethod
    async def get_embeddings_batch(cls, texts: List[str]) -> List[np.ndarray]:
        """
        Embeds a list of texts in batch with bounded concurrency.
        If provider supports array inputs: groups up to 16 chunks / 8000 chars, max 2 concurrent requests.
        If provider is single-string only: dispatches single requests with max 2 concurrent requests.
        """
        if not texts:
            return []

        source = current_embedding_source()
        if source.mode != "remote":
            results = []
            for text in texts:
                key = cls._cache_key(text) if text and text.strip() else None
                if key and key in cls._vector_cache:
                    results.append(cls._vector_cache[key])
                    continue
                vector = cls._get_mock_embedding(text)
                if key and len(cls._vector_cache) < cls._MAX_CACHE_ITEMS:
                    cls._vector_cache[key] = vector
                results.append(vector)
            return results

        compat = await cls.verify_remote_compatibility()
        cls._assert_source(source)
        supports_batch = compat.get("supports_batch", False)

        # Check cache first
        results: List[Optional[np.ndarray]] = [None] * len(texts)
        missing_indices: List[int] = []
        for i, text in enumerate(texts):
            if not text or not text.strip():
                results[i] = np.zeros(compat.get("dimension", cls.DIMENSION), dtype=np.float32)
                continue
            k = cls._cache_key(text)
            if k in cls._vector_cache:
                results[i] = cls._vector_cache[k]
            else:
                missing_indices.append(i)

        if not missing_indices:
            return [r for r in results if r is not None]

        concurrency_limiter = asyncio.Semaphore(2)

        if supports_batch:
            # Group missing texts into batches (max 16 chunks, max 8000 chars)
            batches: List[List[int]] = []
            curr_batch: List[int] = []
            curr_chars = 0
            for idx in missing_indices:
                t_len = len(texts[idx])
                if curr_batch and (len(curr_batch) >= 16 or curr_chars + t_len > 8000):
                    batches.append(curr_batch)
                    curr_batch = [idx]
                    curr_chars = t_len
                else:
                    curr_batch.append(idx)
                    curr_chars += t_len
            if curr_batch:
                batches.append(curr_batch)

            async def _process_batch(batch_idxs: List[int]):
                async with concurrency_limiter:
                    await GlobalRateLimiter.acquire()
                    try:
                        batch_texts = [texts[idx] for idx in batch_idxs]
                        cls._assert_source(source)
                        vectors = await cls._fetch_remote_batch(batch_texts, source=source)
                        cls._assert_source(source)
                        for idx, vec in zip(batch_idxs, vectors):
                            results[idx] = vec
                            cls._vector_cache[cls._cache_key(texts[idx], source)] = vec
                    finally:
                        GlobalRateLimiter.release()

            await asyncio.gather(*[_process_batch(b) for b in batches])
        else:
            # Single-string concurrent requests (max 2 concurrent)
            async def _process_single(idx: int):
                async with concurrency_limiter:
                    await GlobalRateLimiter.acquire()
                    try:
                        cls._assert_source(source)
                        vec = await cls._get_remote_embedding(texts[idx], source=source)
                        cls._assert_source(source)
                        results[idx] = vec
                        cls._vector_cache[cls._cache_key(texts[idx], source)] = vec
                    finally:
                        GlobalRateLimiter.release()

            await asyncio.gather(*[_process_single(idx) for idx in missing_indices])

        cls._assert_source(source)
        return [r if r is not None else np.zeros(compat.get("dimension", cls.DIMENSION), dtype=np.float32) for r in results]

    @classmethod
    async def _fetch_remote_batch(cls, batch_texts: List[str], *, source: Optional[EmbeddingSource] = None) -> List[np.ndarray]:
        source = source or current_embedding_source()
        cls._assert_source(source)
        base_url = source.base_url
        endpoint = sanitize_endpoint(f"{base_url}/embeddings")
        payload = {
            "input": batch_texts,
            "model": source.model
        }
        headers = {
            "Authorization": f"Bearer {settings.EMBEDDING_API_KEY}",
            "Content-Type": "application/json"
        }
        client = HttpClientProvider.get_client()
        input_chars = sum(len(t) for t in batch_texts)

        with call_context() as cid:
            t_req = time.monotonic()
            current_stage = "request_send"
            status_code = None
            req_id = None
            try:
                log_diagnostic(
                    "request_dispatched",
                    level=logging.INFO,
                    message="远程向量批量请求已发送",
                    service="embedding",
                    model=settings.EMBEDDING_MODEL,
                    endpoint=endpoint,
                    input_chars=input_chars,
                    timeout_config=30.0
                )
                current_stage = "await_headers"
                async with stream_request_context(client, "POST", f"{base_url}/embeddings", json=payload, headers=headers, timeout=30.0) as res:
                    headers_ms = (time.monotonic() - t_req) * 1000.0
                    status_code = res.status_code
                    req_id = extract_provider_request_id(res.headers)
                    log_diagnostic(
                        "response_headers",
                        level=logging.DEBUG,
                        message="收到远程向量响应头",
                        service="embedding",
                        model=settings.EMBEDDING_MODEL,
                        status_code=status_code,
                        provider_request_id=req_id,
                        duration_ms=round(headers_ms, 2)
                    )
                    current_stage = "read_body"
                    t_body = time.monotonic()
                    body_bytes = await res.aread()
                    body_ms = (time.monotonic() - t_body) * 1000.0
                    log_diagnostic(
                        "response_body",
                        level=logging.DEBUG,
                        message="远程向量响应体读取完成",
                        service="embedding",
                        model=settings.EMBEDDING_MODEL,
                        status_code=status_code,
                        provider_request_id=req_id,
                        duration_ms=round(body_ms, 2),
                        input_bytes=len(body_bytes)
                    )
                    current_stage = "http_status"
                    res.raise_for_status()

                    current_stage = "parse_response"
                    t_parse = time.monotonic()
                    data = json.loads(body_bytes.decode("utf-8"))
                    raw_items = data.get("data", [])
                    raw_items.sort(key=lambda x: x.get("index", 0))
                    vectors: List[np.ndarray] = []
                    for item in raw_items:
                        arr = np.array(item["embedding"], dtype=np.float32)
                        norm = np.linalg.norm(arr)
                        if norm > 0:
                            arr = arr / norm
                        vectors.append(arr)
                    if len(vectors) != len(batch_texts):
                        raise RuntimeError(f"Embedding 服务返回条目数 ({len(vectors)}) 与输入批次大小 ({len(batch_texts)}) 不匹配")

                    total_ms = (time.monotonic() - t_req) * 1000.0
                    log_diagnostic(
                        "response_parsed",
                        level=logging.INFO,
                        message="远程向量批量解析完成",
                        service="embedding",
                        model=settings.EMBEDDING_MODEL,
                        status_code=status_code,
                        provider_request_id=req_id,
                        duration_ms=round(total_ms, 2)
                    )
                    return vectors
            except Exception as e:
                failed_stage = "connect" if isinstance(e, (httpx.ConnectTimeout, httpx.ConnectError)) else current_stage
                log_diagnostic(
                    "request_failed",
                    level=logging.ERROR,
                    message="远程向量批量服务请求失败",
                    service="embedding",
                    model=settings.EMBEDDING_MODEL,
                    endpoint=endpoint,
                    status_code=status_code,
                    provider_request_id=req_id,
                    failed_stage=failed_stage,
                    exception_type=type(e).__name__
                )
                safe_info = extract_safe_exception_info(e, stage=failed_stage)
                raise RuntimeError(f"远程向量服务请求失败: {safe_info['exception_type']}") from e

    @classmethod
    async def _get_remote_embedding(cls, text: str, *, source: Optional[EmbeddingSource] = None) -> np.ndarray:
        source = source or current_embedding_source()
        cls._assert_source(source)
        if not settings.EMBEDDING_API_KEY:
            raise RuntimeError("Embedding 未配置 API 密钥")
        base_url = source.base_url
        endpoint = sanitize_endpoint(f"{base_url}/embeddings")
        payload = {
            "input": text,
            "model": source.model
        }
        headers = {
            "Authorization": f"Bearer {settings.EMBEDDING_API_KEY}",
            "Content-Type": "application/json"
        }
        client = HttpClientProvider.get_client()

        with call_context() as cid:
            t_req = time.monotonic()
            current_stage = "request_send"
            status_code = None
            req_id = None
            try:
                log_diagnostic(
                    "request_dispatched",
                    level=logging.INFO,
                    message="远程单条向量请求已发送",
                    service="embedding",
                    model=settings.EMBEDDING_MODEL,
                    endpoint=endpoint,
                    input_chars=len(text),
                    timeout_config=30.0
                )
                current_stage = "await_headers"
                async with stream_request_context(client, "POST", f"{base_url}/embeddings", json=payload, headers=headers, timeout=30.0) as res:
                    headers_ms = (time.monotonic() - t_req) * 1000.0
                    status_code = res.status_code
                    req_id = extract_provider_request_id(res.headers)
                    log_diagnostic(
                        "response_headers",
                        level=logging.DEBUG,
                        message="收到远程单条向量响应头",
                        service="embedding",
                        model=settings.EMBEDDING_MODEL,
                        status_code=status_code,
                        provider_request_id=req_id,
                        duration_ms=round(headers_ms, 2)
                    )
                    current_stage = "read_body"
                    t_body = time.monotonic()
                    body_bytes = await res.aread()
                    body_ms = (time.monotonic() - t_body) * 1000.0
                    log_diagnostic(
                        "response_body",
                        level=logging.DEBUG,
                        message="远程单条向量响应体读取完成",
                        service="embedding",
                        model=settings.EMBEDDING_MODEL,
                        status_code=status_code,
                        provider_request_id=req_id,
                        duration_ms=round(body_ms, 2),
                        input_bytes=len(body_bytes)
                    )
                    current_stage = "http_status"
                    res.raise_for_status()

                    current_stage = "parse_response"
                    data = json.loads(body_bytes.decode("utf-8"))
                    raw_vec = data["data"][0]["embedding"]
                    arr = np.array(raw_vec, dtype=np.float32)
                    norm = np.linalg.norm(arr)
                    if norm > 0:
                        arr = arr / norm

                    total_ms = (time.monotonic() - t_req) * 1000.0
                    log_diagnostic(
                        "response_parsed",
                        level=logging.INFO,
                        message="远程单条向量解析完成",
                        service="embedding",
                        model=settings.EMBEDDING_MODEL,
                        status_code=status_code,
                        provider_request_id=req_id,
                        duration_ms=round(total_ms, 2)
                    )
                    return arr
            except Exception as e:
                failed_stage = "connect" if isinstance(e, (httpx.ConnectTimeout, httpx.ConnectError)) else current_stage
                log_diagnostic(
                    "request_failed",
                    level=logging.ERROR,
                    message="远程单条向量请求失败",
                    service="embedding",
                    model=settings.EMBEDDING_MODEL,
                    endpoint=endpoint,
                    status_code=status_code,
                    provider_request_id=req_id,
                    failed_stage=failed_stage,
                    exception_type=type(e).__name__
                )
                safe_info = extract_safe_exception_info(e, stage=failed_stage)
                raise RuntimeError(f"远程向量服务请求失败: {safe_info['exception_type']}") from e

    @classmethod
    def _get_mock_embedding(cls, text: str) -> np.ndarray:
        """
        Deterministic pseudo-embedding for local offline development.
        Hashes text tokens to dimension bins and normalizes.
        """
        vec = np.zeros(cls.DIMENSION, dtype=np.float32)
        words = text.split()
        if not words:
            words = [text]

        for w in words:
            h = int(hashlib.md5(w.encode("utf-8")).hexdigest(), 16)
            idx = h % cls.DIMENSION
            sign = 1.0 if (h >> 8) % 2 == 0 else -1.0
            vec[idx] += sign

        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        return vec

    @classmethod
    def serialize_embedding(cls, vec: np.ndarray) -> bytes:
        return vec.astype(np.float32).tobytes()

    @classmethod
    def deserialize_embedding(cls, blob: bytes) -> np.ndarray:
        return np.frombuffer(blob, dtype=np.float32)
