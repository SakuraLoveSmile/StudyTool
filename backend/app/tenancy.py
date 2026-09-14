"""Server-owned tenant identity; never derive storage paths from request input."""
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import UUID

_tenant: ContextVar[str | None] = ContextVar("rag_tenant", default=None)


def current_tenant_id() -> str | None:
    return _tenant.get()


@contextmanager
def tenant_scope(user_id: str):
    canonical = str(UUID(user_id))
    if canonical != user_id:
        raise ValueError("Tenant ID must be a canonical UUID")
    token = _tenant.set(canonical)
    try:
        yield canonical
    finally:
        _tenant.reset(token)
