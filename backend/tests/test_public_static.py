"""Bundled web UI remains public while API/session boundaries stay private."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth
from app.access import AccessMiddleware
from app.config import settings
from app.desktop import DesktopAccessMiddleware, setup_desktop_static_serving


ORIGIN = "https://testserver"


@pytest.fixture()
def web_dist(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>synthetic login shell</html>")
    (dist / "assets" / "app.js").write_text("console.log('synthetic asset')")
    (dist / "favicon.svg").write_text("<svg></svg>")
    (tmp_path / "secret.txt").write_text("private backend secret")
    monkeypatch.setattr(settings, "FRONTEND_DIST_DIR", str(dist))
    monkeypatch.setattr(settings, "PUBLIC_MODE", True)
    monkeypatch.setattr(settings, "PUBLIC_ORIGIN", ORIGIN)
    monkeypatch.setattr(settings, "DESKTOP_MODE", False)
    auth.init_auth_db()
    return dist


def _web_app():
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/private-export")
    @app.get("/assets/private-export")
    def private_export():
        return {"private": True}

    @app.get("/api/private")
    @app.post("/api/private")
    def private_api():
        return {"private": True}

    app.add_middleware(AccessMiddleware)
    app.add_middleware(DesktopAccessMiddleware)
    setup_desktop_static_serving(app)
    return app


@pytest.mark.parametrize("path", ["/", "/library/books/123", "/favicon.svg", "/assets/app.js"])
def test_anonymous_can_load_only_frontend_get_and_head(web_dist, path):
    with TestClient(_web_app(), base_url=ORIGIN) as client:
        response = client.get(path)
        assert response.status_code == 200
        assert response.content
        head = client.head(path)
        assert head.status_code == 200
        assert head.content == b""
        assert head.headers["content-length"] == response.headers["content-length"]


@pytest.mark.parametrize("path", ["/api", "/api/private", "/api/missing", "/private-export", "/assets/private-export"])
def test_anonymous_static_does_not_publish_backend_routes(web_dist, path):
    with TestClient(_web_app(), base_url=ORIGIN) as client:
        response = client.get(path)
        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "AUTH_REQUIRED"


@pytest.mark.parametrize("path", ["/docs", "/docs/oauth2-redirect", "/redoc", "/redoc/anything", "/openapi.json", "/openapi.json/anything"])
def test_public_docs_stay_hidden(web_dist, path):
    with TestClient(_web_app(), base_url=ORIGIN) as client:
        assert client.get(path).status_code == 404


def test_static_paths_do_not_escape_bundle(web_dist):
    (web_dist / "outside.txt").symlink_to(web_dist.parent / "secret.txt")
    (web_dist / "assets" / "outside.txt").symlink_to(web_dist.parent / "secret.txt")
    with TestClient(_web_app(), base_url=ORIGIN) as client:
        for path in ["/%2e%2e/secret.txt", "/outside.txt", "/assets/outside.txt", "/%00"]:
            response = client.get(path)
            assert response.status_code in {403, 404}, (path, response.text)
            assert "private backend secret" not in response.text
        (web_dist / "index.html").unlink()
        (web_dist / "index.html").symlink_to(web_dist.parent / "secret.txt")
        response = client.get("/library")
        assert response.status_code in {403, 404}
        assert "private backend secret" not in response.text


def test_assets_directory_cannot_mount_files_outside_bundle(web_dist, tmp_path):
    (web_dist / "assets" / "app.js").unlink()
    (web_dist / "assets").rmdir()
    (web_dist / "assets").symlink_to(tmp_path, target_is_directory=True)
    with TestClient(_web_app(), base_url=ORIGIN) as client:
        response = client.get("/assets/secret.txt")
        assert response.status_code in {403, 404}
        assert "private backend secret" not in response.text


def test_public_static_does_not_bypass_write_origin_session_or_csrf(web_dist):
    user_id = auth.provision_user("synthetic-user", "synthetic-password-123")
    with auth._db() as conn:
        conn.execute("UPDATE users SET must_change_password=0 WHERE id=?", (user_id,))
    token, csrf = auth.create_session(user_id)
    with TestClient(_web_app(), base_url=ORIGIN) as client:
        for path in ["/", "/assets/app.js", "/library", "/api/private"]:
            assert client.post(path).json()["detail"]["code"] == "ORIGIN_REJECTED"
            response = client.post(path, headers={"Origin": ORIGIN})
            assert response.status_code == 401
        client.cookies.set("rag_session", token)
        for path in ["/", "/assets/app.js", "/api/private"]:
            response = client.post(path, headers={"Origin": ORIGIN})
            assert response.status_code == 403
            assert response.json()["detail"]["code"] == "CSRF_REJECTED"
        headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
        assert client.post("/api/private", headers=headers).status_code == 200
        assert client.post("/assets/app.js", headers=headers).status_code == 405
        assert client.post("/", headers=headers).status_code == 405


@pytest.mark.parametrize("explicit", [None, "missing-directory"])
def test_implicit_or_missing_bundle_does_not_enable_anonymous_access(web_dist, monkeypatch, explicit):
    from app import desktop
    monkeypatch.setattr(settings, "FRONTEND_DIST_DIR", explicit)
    # Model the existing development/bundled fallback without touching repo files.
    monkeypatch.setattr(desktop, "get_frontend_dist_dir", lambda: web_dist)
    with TestClient(_web_app(), base_url=ORIGIN) as client:
        assert client.get("/").status_code == 401
        assert client.get("/assets/app.js").status_code == 401


def test_desktop_static_retains_host_and_api_token_boundary(web_dist, monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_MODE", False)
    monkeypatch.setattr(settings, "DESKTOP_MODE", True)
    monkeypatch.setattr(settings, "DESKTOP_INSTANCE_TOKEN", "synthetic-desktop-token")
    with TestClient(_web_app()) as client:
        assert client.get("/").status_code == 200
        assert client.head("/library").status_code == 200
        assert client.get("/", headers={"Host": "attacker.example"}).status_code == 403
        assert client.get("/api/private").status_code == 401
        response = client.get("/api/private", headers={"X-StudyTool-Instance-Token": "synthetic-desktop-token"})
        assert response.status_code == 200
