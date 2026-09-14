"""Authentication boundary wraps the entire ASGI response, including file streams."""
import hmac

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.tenancy import tenant_scope


class AccessMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not settings.PUBLIC_MODE:
            await self.app(scope, receive, send)
            return
        from app.auth import authenticate_session, csrf_token
        request = Request(scope)
        path = scope["path"]

        async def reject(status, code, message):
            response = JSONResponse({"detail": {"code": code, "message": message}}, status_code=status)
            await response(scope, receive, send)

        if path == "/health" and request.method in {"GET", "HEAD"}:
            await self.app(scope, receive, send)
            return
        if path in {"/docs", "/redoc", "/openapi.json"} or path.startswith("/docs/"):
            await reject(404, "NOT_FOUND", "页面不存在")
            return
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("origin") != settings.PUBLIC_ORIGIN.rstrip("/"):
                await reject(403, "ORIGIN_REJECTED", "请求来源不被允许")
                return
        if path == "/api/auth/login" and request.method == "POST":
            await self.app(scope, receive, send)
            return
        token = request.cookies.get("rag_session", "")
        user = await run_in_threadpool(authenticate_session, token) if token else None
        if not user:
            await reject(401, "AUTH_REQUIRED", "请先登录")
            return
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            supplied = request.headers.get("x-csrf-token", "")
            if not supplied or not hmac.compare_digest(supplied, csrf_token(token)):
                await reject(403, "CSRF_REJECTED", "会话验证失败，请刷新后重试")
                return
        if user["must_change_password"] and path not in {
            "/api/auth/me", "/api/auth/logout", "/api/auth/change-password"
        }:
            await reject(403, "PASSWORD_CHANGE_REQUIRED", "请先修改初始密码")
            return
        scope.setdefault("state", {})["user"] = user
        with tenant_scope(user["id"]):
            async def private_send(message):
                if message["type"] == "http.response.start":
                    headers = list(message.get("headers", []))
                    headers.append((b"cache-control", b"no-store"))
                    message = {**message, "headers": headers}
                await send(message)
            await self.app(scope, receive, private_send)
