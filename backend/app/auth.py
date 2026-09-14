"""Authentication storage and session primitives.

The account database intentionally lives beside the per-user data directories,
while callers provide the tenant scope before opening a user's business DB.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.config import settings

SESSION_TTL = 7 * 24 * 60 * 60
MIN_PASSWORD_LENGTH = 12
_hasher = PasswordHasher()
_dummy_hash = _hasher.hash("dummy-password-for-timing")


def _root_path() -> Path:
    value = getattr(settings, "root_data_path", None)
    if value is not None:
        return Path(value)
    return Path(getattr(settings, "DATA_DIR", "./data")).resolve()


@contextmanager
def _db():
    root = _root_path()
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "accounts.db", timeout=30.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_auth_db() -> None:
    with _db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
              password_hash TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
              must_change_password INTEGER NOT NULL DEFAULT 1,
              provisioned INTEGER NOT NULL DEFAULT 0,
              created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
              token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
              csrf_hash TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
              revoked_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions(user_id);
            CREATE TABLE IF NOT EXISTS login_failures (
              subject TEXT NOT NULL, failed_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS login_failures_idx ON login_failures(subject, failed_at);
            """
        )
        if "provisioned" not in {r[1] for r in conn.execute("PRAGMA table_info(users)") }:
            conn.execute("ALTER TABLE users ADD COLUMN provisioned INTEGER NOT NULL DEFAULT 1")


def _check_password(password: str) -> None:
    if not isinstance(password, str) or not MIN_PASSWORD_LENGTH <= len(password) <= 1024:
        raise ValueError(f"password must be {MIN_PASSWORD_LENGTH} to 1024 characters")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def csrf_token(token: str) -> str:
    """Return the deterministic CSRF proof for an opaque session token."""
    return hmac.new(token.encode("utf-8"), b"csrf", hashlib.sha256).hexdigest()


def _user(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "username": row["username"], "must_change_password": bool(row["must_change_password"])}


def provision_user(username: str, password: str) -> str:
    username = username.strip()
    if not username or len(username) > 256:
        raise ValueError("username must be 1 to 256 characters")
    _check_password(password)
    init_auth_db()
    user_id = str(uuid.uuid4())
    now = int(time.time())
    with _db() as conn:
        existing = _get_user(conn, username)
        if existing:
            if existing["provisioned"] or existing["enabled"]:
                raise ValueError("user already exists")
            user_id = existing["id"]
            conn.execute("UPDATE users SET password_hash=? WHERE id=?", (_hasher.hash(password), user_id))
        else:
            conn.execute("INSERT INTO users(id, username, password_hash, created_at) VALUES(?,?,?,?)",
                         (user_id, username, _hasher.hash(password), now))
    try:
        from app.database import init_db
        from app.tenancy import tenant_scope
        with tenant_scope(user_id):
            init_db()
    except Exception:
        raise
    with _db() as conn:
        conn.execute("UPDATE users SET enabled=1, provisioned=1 WHERE id=?", (user_id,))
    return user_id


def _get_user(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()


def _revoke_user_sessions(conn: sqlite3.Connection, user_id: str, now: int | None = None) -> None:
    conn.execute("UPDATE sessions SET revoked_at = COALESCE(revoked_at, ?) WHERE user_id = ?", (now or int(time.time()), user_id))


def disable_user(username: str) -> None:
    with _db() as conn:
        row = _get_user(conn, username)
        if row is None:
            raise ValueError("user not found")
        conn.execute("UPDATE users SET enabled=0 WHERE id=?", (row["id"],))
        _revoke_user_sessions(conn, row["id"])


def reset_password(username: str, password: str) -> None:
    _check_password(password)
    with _db() as conn:
        row = _get_user(conn, username)
        if row is None:
            raise ValueError("user not found")
        conn.execute("UPDATE users SET password_hash=?, must_change_password=1 WHERE id=?", (_hasher.hash(password), row["id"]))
        _revoke_user_sessions(conn, row["id"])


def change_password(user_id: str, current_password: str, new_password: str) -> str:
    _check_password(new_password)
    if current_password == new_password:
        raise ValueError("new password must differ from current password")
    with _db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=? AND enabled=1", (user_id,)).fetchone()
        if row is None or not _verify(row["password_hash"], current_password):
            raise ValueError("invalid credentials")
        conn.execute("UPDATE users SET password_hash=?, must_change_password=0 WHERE id=?", (_hasher.hash(new_password), user_id))
        _revoke_user_sessions(conn, user_id)
    return create_session(user_id)[0]


def _verify(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def enabled_user_ids() -> list[str]:
    with _db() as conn:
        return [r[0] for r in conn.execute("SELECT id FROM users WHERE enabled=1 ORDER BY username")]


def authenticate(username: str, password: str, ip: str = "") -> dict[str, Any] | None:
    if len(username) > 256 or len(password) > 1024:
        return None
    now = int(time.time())
    subjects = [f"a:{username.strip().lower()}", f"i:{ip[:128]}"]
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cutoff = now - 900
        counts = [conn.execute("SELECT COUNT(*) FROM login_failures WHERE failed_at > ? AND subject = ?", (cutoff, s)).fetchone()[0] for s in subjects]
        if any(count >= 5 for count in counts):
            raise PermissionError("rate limited")
        row = _get_user(conn, username)
        valid = row is not None and bool(row["enabled"]) and _verify(row["password_hash"], password)
        if row is None or not row["enabled"]:
            _verify(_dummy_hash, password)
        if not valid:
            conn.executemany("INSERT INTO login_failures(subject, failed_at) VALUES(?,?)", [(s, now) for s in subjects])
            conn.execute("DELETE FROM login_failures WHERE failed_at <= ?", (cutoff,))
            return None
        conn.execute("DELETE FROM login_failures WHERE subject IN (?,?)", tuple(subjects))
        return _user(row)


def create_session(user_id: str) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    csrf = csrf_token(token)
    now = int(time.time())
    with _db() as conn:
        conn.execute("INSERT INTO sessions(token_hash,user_id,csrf_hash,created_at,expires_at) VALUES(?,?,?,?,?)",
                     (_hash_token(token), user_id, _hash_token(csrf), now, now + SESSION_TTL))
    return token, csrf


def authenticate_session(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    now = int(time.time())
    with _db() as conn:
        row = conn.execute("SELECT u.*, s.expires_at, s.revoked_at FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?", (_hash_token(token),)).fetchone()
        if row is None or row["revoked_at"] is not None or row["expires_at"] <= now or not row["enabled"]:
            return None
        return _user(row)


def revoke_session(token: str | None) -> None:
    if token:
        with _db() as conn:
            conn.execute("UPDATE sessions SET revoked_at=? WHERE token_hash=?", (int(time.time()), _hash_token(token)))
