import asyncio
import logging
from typing import Optional
import httpx

logger = logging.getLogger(__name__)

class HttpClientProvider:
    """Singleton provider for shared httpx.AsyncClient with connection pooling."""
    _client: Optional[httpx.AsyncClient] = None

    @classmethod
    async def start(cls):
        if cls._client is None or cls._client.is_closed:
            limits = httpx.Limits(
                max_keepalive_connections=20,
                max_connections=50,
                keepalive_expiry=30.0
            )
            cls._client = httpx.AsyncClient(
                limits=limits,
                timeout=httpx.Timeout(60.0, connect=15.0)
            )
            logger.info("Shared HttpClientProvider initialized with connection pooling")

    @classmethod
    async def close(cls):
        if cls._client and not cls._client.is_closed:
            try:
                await cls._client.aclose()
            except RuntimeError as exc:
                # Starlette TestClient may close a lifespan loop before a
                # client created by a previous test loop is torn down.  The
                # transport is already unusable; do not mask the test/request
                # result with a shutdown-only loop error.
                if "Event loop is closed" not in str(exc):
                    raise
                logger.debug("HttpClientProvider transport closed after event loop shutdown")
            finally:
                cls._client = None
            logger.info("Shared HttpClientProvider closed")

    @classmethod
    def get_client(cls) -> httpx.AsyncClient:
        if cls._client is None or cls._client.is_closed:
            limits = httpx.Limits(
                max_keepalive_connections=20,
                max_connections=50,
                keepalive_expiry=30.0
            )
            cls._client = httpx.AsyncClient(
                limits=limits,
                timeout=httpx.Timeout(60.0, connect=15.0)
            )
        return cls._client


class GlobalRateLimiter:
    """
    Global shared concurrency limiter across external AI/OCR/Embedding calls.
    Prevents overwhelming provider quotas and coordinates 429 backoffs.
    """
    _semaphore: Optional[asyncio.Semaphore] = None
    _max_concurrency: int = 4
    _backoff_until: float = 0.0
    _lock: Optional[asyncio.Lock] = None

    @classmethod
    def _init(cls):
        if cls._semaphore is None:
            cls._semaphore = asyncio.Semaphore(cls._max_concurrency)
            cls._lock = asyncio.Lock()

    @classmethod
    async def acquire(cls):
        cls._init()
        # If currently under 429 backoff cooldown, wait it out
        now = asyncio.get_running_loop().time()
        if cls._backoff_until > now:
            wait_time = cls._backoff_until - now
            logger.warning("GlobalRateLimiter active backoff: waiting %.2fs", wait_time)
            await asyncio.sleep(wait_time)

        await cls._semaphore.acquire()

    @classmethod
    def release(cls):
        cls._init()
        cls._semaphore.release()

    @classmethod
    async def report_429(cls, backoff_seconds: float = 5.0):
        cls._init()
        async with cls._lock:
            now = asyncio.get_running_loop().time()
            cls._backoff_until = max(cls._backoff_until, now + backoff_seconds)
            logger.warning("GlobalRateLimiter received 429: backing off for %.1fs", backoff_seconds)
