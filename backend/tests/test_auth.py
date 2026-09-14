from __future__ import annotations

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth
from app.config import settings
from app.routers.auth import router


@pytest.fixture()
def auth_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "AUTH_COOKIE_SECURE", False, raising=False)
    monkeypatch.setattr(settings, "PUBLIC_MODE", True, raising=False)
    auth.init_auth_db()
    yield tmp_path


def test_provision_initializes_and_session_is_revoked(auth_env):
    user_id = auth.provision_user("alice", "a" * 12)
    assert user_id in auth.enabled_user_ids()
    user = auth.authenticate("alice", "a" * 12, "127.0.0.1")
    assert user["id"] == user_id
    token, csrf = auth.create_session(user_id)
    assert auth.authenticate_session(token)["username"] == "alice"
    assert csrf == auth.csrf_token(token)
    auth.disable_user("alice")
    assert auth.authenticate_session(token) is None


def test_password_policy_and_rate_limit(auth_env):
    with pytest.raises(ValueError):
        auth.provision_user("alice", "short")
    auth.provision_user("alice", "a" * 12)
    with auth._db() as conn:
        conn.execute("UPDATE users SET enabled=1")
    for _ in range(5):
        assert auth.authenticate("alice", "wrong", "10.0.0.1") is None
    with pytest.raises(PermissionError):
        auth.authenticate("alice", "a" * 12, "10.0.0.1")


def test_routes_login_csrf_change_and_logout(auth_env):
    app = FastAPI()
    app.include_router(router)
    user_id = auth.provision_user("alice", "a" * 12)
    with TestClient(app) as client:
        result = client.post("/api/auth/login", json={"username": "alice", "password": "a" * 12})
        assert result.status_code == 200
        payload = result.json()
        assert payload["user"]["must_change_password"] is True
        csrf = payload["csrf_token"]
        assert client.get("/api/auth/me").json()["user"]["username"] == "alice"
        assert client.post("/api/auth/logout").status_code == 403
        changed = client.post("/api/auth/change-password", headers={"X-CSRF-Token": csrf}, json={"current_password": "a" * 12, "new_password": "b" * 12})
        assert changed.status_code == 200
        assert changed.json()["user"]["must_change_password"] is False
        assert client.post("/api/auth/logout", headers={"X-CSRF-Token": changed.json()["csrf_token"]}).json() == {"ok": True}
        assert client.get("/api/auth/me").status_code == 401


def test_expired_session_is_rejected(auth_env):
    user_id = auth.provision_user("alice", "a" * 12)
    with auth._db() as conn:
        conn.execute("UPDATE users SET enabled=1 WHERE id=?", (user_id,))
    token, _ = auth.create_session(user_id)
    with auth._db() as conn:
        conn.execute("UPDATE sessions SET expires_at=0 WHERE token_hash=?", (auth._hash_token(token),))
    assert auth.authenticate_session(token) is None


def test_failed_provision_can_retry_without_enabling_disabled_account(auth_env, monkeypatch):
    from app import database
    original = database.init_db
    def fail():
        raise RuntimeError("synthetic disk failure")
    monkeypatch.setattr(database, "init_db", fail)
    with pytest.raises(RuntimeError):
        auth.provision_user("retry-user", "valid-password-123")
    assert auth.enabled_user_ids() == []
    monkeypatch.setattr(database, "init_db", original)
    user_id = auth.provision_user("retry-user", "valid-password-123")
    assert (auth_env / "users" / user_id / "app.db").is_file()
    auth.disable_user("retry-user")
    auth.reset_password("retry-user", "reset-password-123")
    assert user_id not in auth.enabled_user_ids()
    with pytest.raises(ValueError, match="already exists"):
        auth.provision_user("retry-user", "valid-password-123")


def test_login_throttles_account_across_ips_and_ip_across_accounts(auth_env):
    auth.provision_user("alice", "valid-password-123")
    for i in range(5):
        assert auth.authenticate("alice", "wrong", f"10.0.0.{i}") is None
    with pytest.raises(PermissionError):
        auth.authenticate("alice", "valid-password-123", "10.0.0.99")
    for i in range(5):
        assert auth.authenticate(f"unknown-{i}", "wrong", "10.1.0.1") is None
    with pytest.raises(PermissionError):
        auth.authenticate("unrelated", "wrong", "10.1.0.1")
