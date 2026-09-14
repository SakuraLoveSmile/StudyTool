from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app import auth

router = APIRouter(prefix="/api/auth", tags=["auth"])


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=1024)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=12, max_length=1024)


def current_user(request: Request) -> dict[str, Any]:
    user = auth.authenticate_session(request.cookies.get("rag_session"))
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return user


def _public_write() -> None:
    if not bool(getattr(auth.settings, "PUBLIC_MODE", False)):
        raise HTTPException(status_code=404, detail="页面不存在")


def require_csrf(request: Request) -> None:
    token = request.cookies.get("rag_session")
    expected = request.headers.get("X-CSRF-Token")
    if not token or not expected or not hmac_compare(expected, auth.csrf_token(token)):
        raise HTTPException(status_code=403, detail="csrf validation failed")


def hmac_compare(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(a, b)


def _payload(user: dict[str, Any], csrf: str) -> dict[str, Any]:
    return {"user": user, "csrf_token": csrf}


@router.post("/login")
def login(body: Credentials, request: Request, response: Response):
    _public_write()
    response.headers["Cache-Control"] = "no-store"
    try:
        user = auth.authenticate(body.username, body.password, request.client.host if request.client else "")
    except PermissionError:
        raise HTTPException(status_code=429, detail="too many login attempts")
    if user is None:
        raise HTTPException(status_code=401, detail="invalid credentials")
    token, csrf = auth.create_session(user["id"])
    secure = bool(getattr(auth.settings, "AUTH_COOKIE_SECURE", True))
    response.set_cookie("rag_session", token, max_age=auth.SESSION_TTL, httponly=True, secure=secure, samesite="lax", path="/")
    return _payload(user, csrf)


@router.get("/me")
def me(request: Request):
    user = auth.authenticate_session(request.cookies.get("rag_session"))
    if user is None and not bool(getattr(auth.settings, "PUBLIC_MODE", False)):
        return {"auth_required": False, "user": None, "csrf_token": None}
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    token = request.cookies["rag_session"]
    return _payload(user, auth.csrf_token(token))


@router.post("/logout")
def logout(request: Request, response: Response):
    _public_write()
    current_user(request); require_csrf(request)
    auth.revoke_session(request.cookies.get("rag_session"))
    response.delete_cookie("rag_session", path="/")
    return {"ok": True}


@router.post("/change-password")
def change_password(body: PasswordChange, request: Request, response: Response):
    _public_write()
    user = current_user(request); require_csrf(request)
    try:
        token = auth.change_password(user["id"], body.current_password, body.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    csrf = auth.csrf_token(token)
    secure = bool(getattr(auth.settings, "AUTH_COOKIE_SECURE", True))
    response.set_cookie("rag_session", token, max_age=auth.SESSION_TTL, httponly=True, secure=secure, samesite="lax", path="/")
    fresh = auth.authenticate_session(token)
    return _payload(fresh, csrf)
