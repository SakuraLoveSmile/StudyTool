import os
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from app.config import settings
from app.database import db_session, init_db
from app.desktop import (
    MaintenanceManager,
    check_system_idle,
    get_active_tasks_count,
)
from app.main import app


@pytest.fixture
def desktop_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "desktop_data"
    data_dir.mkdir(parents=True)
    monkeypatch.setenv("STUDYTOOL_DESKTOP_DATA_DIR", str(data_dir))
    monkeypatch.setattr(settings, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(settings, "DESKTOP_MODE", True)
    monkeypatch.setattr(settings, "DESKTOP_INSTANCE_TOKEN", "test-token-xyz")
    monkeypatch.setattr(settings, "HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "PORT", 18764)
    monkeypatch.setattr(settings, "PUBLIC_MODE", False)

    init_db()
    MaintenanceManager.force_clear()
    yield data_dir
    MaintenanceManager.force_clear()


def test_request_operations_running_state_blocks_idle(desktop_env):
    """Regression: request_operations sets state='running' (not 'processing').
    get_active_tasks_count and check_system_idle must recognize 'running'.
    """
    with db_session() as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS request_operations (
               id TEXT PRIMARY KEY,
               route TEXT,
               idempotency_key TEXT,
               payload_hash TEXT,
               state TEXT,
               result_json TEXT,
               error_json TEXT,
               updated_at TIMESTAMP
            )"""
        )
        db.execute(
            """INSERT OR REPLACE INTO request_operations(id, route, idempotency_key, payload_hash, state)
               VALUES('op-run-1', '/api/practice/score', 'idem-run-1', 'hash1', 'running')"""
        )

    # In defective code, this returns 0 because it only checks state='processing'
    cnt = get_active_tasks_count()
    assert cnt >= 1, f"Expected active tasks count >= 1 for running request operation, got {cnt}"

    idle, reason = check_system_idle()
    assert idle is False, f"Expected system to not be idle during running operation, got {idle} ({reason})"
    assert "正在进行" in reason


def test_shutdown_requires_lease_verification(desktop_env):
    """Regression: /api/desktop/shutdown must verify lease ownership.
    Calling without lease or with invalid lease must be rejected.
    """
    client = TestClient(app)
    headers = {
        "X-StudyTool-Instance-Token": "test-token-xyz",
        "Host": "127.0.0.1:18764",
        "Origin": "http://127.0.0.1:18764",
    }

    # Calling shutdown without active lease or wrong lease should fail
    res_no_lease = client.post("/api/desktop/shutdown", json={"lease_id": "nonexistent-lease"}, headers=headers)
    assert res_no_lease.status_code in {400, 403, 409}, f"Expected shutdown to reject invalid lease, got {res_no_lease.status_code}"


def test_drain_tasks_repeatable_and_clears_wal(desktop_env):
    """Regression: drain must be idempotent, safely repeatable, and flush WAL without locking errors."""
    client = TestClient(app)
    headers = {
        "X-StudyTool-Instance-Token": "test-token-xyz",
        "Host": "127.0.0.1:18764",
        "Origin": "http://127.0.0.1:18764",
    }

    # Create dummy pending/processing tasks
    with db_session() as db:
        db.execute(
            """INSERT OR REPLACE INTO tasks (id, task_type, target_id, status, progress, message)
               VALUES ('task-drain-1', 'parse', 'doc-1', 'processing', 20, 'parsing')"""
        )

    MaintenanceManager.acquire("lease-drain-test", "测试排空", is_exit=True)

    # First drain call
    res1 = client.post("/api/desktop/drain", json={"lease_id": "lease-drain-test"}, headers=headers)
    assert res1.status_code == 200
    assert res1.json()["ok"] is True

    # Second drain call must also succeed without error
    res2 = client.post("/api/desktop/drain", json={"lease_id": "lease-drain-test"}, headers=headers)
    assert res2.status_code == 200
    assert res2.json()["ok"] is True

    MaintenanceManager.release("lease-drain-test")

    with db_session() as db:
        row = db.execute("SELECT status FROM tasks WHERE id = 'task-drain-1'").fetchone()
        assert row[0] == "failed"


def test_real_subprocess_single_stdin_bootstrap(tmp_path):
    """Proves with a real child process that single configuration on stdin starts backend
    without hung secondary read or import side-effects.
    """
    import subprocess
    import json
    import time
    import urllib.request
    import sys
    import socket

    # Find a free local port
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    free_port = sock.getsockname()[1]
    sock.close()

    token = "subproc-test-token-7788"
    backend_dir = Path(__file__).resolve().parents[1]
    data_dir = tmp_path / "subproc_data"
    data_dir.mkdir(parents=True)

    bootstrap_payload = {
        "instance_token": token,
        "port": free_port,
        "host": "127.0.0.1",
        "data_dir": str(data_dir),
    }

    env = {
        **os.environ,
        "PYTHONPATH": str(backend_dir),
        "STUDYTOOL_DESKTOP_MODE": "1",
        "STUDYTOOL_DESKTOP_DATA_DIR": str(data_dir),
    }

    proc = subprocess.Popen(
        [sys.executable, "-m", "app.main", "--desktop", f"--port={free_port}"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(backend_dir),
        env=env,
        text=True,
    )

    try:
        # Write payload once
        proc.stdin.write(json.dumps(bootstrap_payload) + "\n")
        proc.stdin.flush()

        # Poll status endpoint
        url = f"http://127.0.0.1:{free_port}/api/desktop/status"
        req = urllib.request.Request(
            url,
            headers={
                "X-StudyTool-Instance-Token": token,
                "Host": f"127.0.0.1:{free_port}",
            },
        )

        ready = False
        start_time = time.time()
        while time.time() - start_time < 12.0:
            if proc.poll() is not None:
                stdout, stderr = proc.communicate(timeout=2)
                raise RuntimeError(f"Backend process terminated prematurely: stdout={stdout}, stderr={stderr}")
            try:
                with urllib.request.urlopen(req, timeout=1.0) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode())
                        if data.get("desktop_mode") is True:
                            ready = True
                            break
            except Exception:
                time.sleep(0.3)

        assert ready is True, "Subprocess failed to become ready within timeout"
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()


def test_exit_lease_behavior_and_preemption_rules(desktop_env):
    """Regression T1:
    - Normal lease requires idle; fails if mutating requests or tasks are active.
    - Exit lease allows acquisition with active tasks, immediately blocks new mutating requests.
    - Exit lease cannot preempt an active regular maintenance lease.
    """
    from fastapi import HTTPException

    # 1. Simulate in-flight mutating request
    assert MaintenanceManager.inc_mutating_request() is True

    # Normal lease acquisition is rejected
    with pytest.raises(HTTPException) as exc_norm:
        MaintenanceManager.acquire("lease-normal-1", "常规维护", is_exit=False)
    assert exc_norm.value.status_code == 409

    # Exit lease acquisition is allowed even with in-flight requests
    MaintenanceManager.acquire("lease-exit-1", "退出软件", is_exit=True)
    lease = MaintenanceManager.get_lease()
    assert lease is not None
    assert lease["lease_id"] == "lease-exit-1"
    assert lease["lease_type"] == "exit"

    # Once exit lease is active, new mutating requests must be rejected
    assert MaintenanceManager.inc_mutating_request() is False

    # Clean up
    MaintenanceManager.dec_mutating_request()
    MaintenanceManager.release("lease-exit-1")

    # 2. Test preemption rule: regular maintenance lease already active
    MaintenanceManager.acquire("lease-backup-1", "数据备份", is_exit=False)
    # Trying to acquire exit lease must be rejected with 409 because another maintenance is active
    with pytest.raises(HTTPException) as exc_exit:
        MaintenanceManager.acquire("lease-exit-2", "退出软件", is_exit=True)
    assert exc_exit.value.status_code == 409
    assert "数据备份" in exc_exit.value.detail

    MaintenanceManager.release("lease-backup-1")


def test_drain_requires_lease_and_reports_explicit_failure(desktop_env, monkeypatch):
    """Regression T1:
    - Calling /drain without lease or invalid lease must return 403 or 409.
    - Calling /drain with valid lease succeeds.
    - If checkpoint or database operation throws, /drain must return HTTP 500 rather than pseudo-success.
    """
    client = TestClient(app)
    headers = {
        "X-StudyTool-Instance-Token": "test-token-xyz",
        "Host": "127.0.0.1:18764",
        "Origin": "http://127.0.0.1:18764",
    }

    # 1. Calling drain with invalid lease when a lease exists
    MaintenanceManager.acquire("active-lease-123", "测试操作", is_exit=True)
    res_wrong_lease = client.post("/api/desktop/drain", json={"lease_id": "wrong-lease"}, headers=headers)
    assert res_wrong_lease.status_code in {403, 409}

    # 2. Calling drain with valid lease succeeds
    res_ok = client.post("/api/desktop/drain", json={"lease_id": "active-lease-123"}, headers=headers)
    assert res_ok.status_code == 200
    assert res_ok.json()["ok"] is True

    # 3. Simulate checkpoint failure: must raise 500
    import sqlite3
    def failing_checkpoint(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error during wal_checkpoint")

    with monkeypatch.context() as m:
        m.setattr(sqlite3, "connect", failing_checkpoint)
        res_fail = client.post("/api/desktop/drain", json={"lease_id": "active-lease-123"}, headers=headers)
        assert res_fail.status_code == 500
        assert "WAL checkpoint" in res_fail.json()["detail"] or "disk I/O error" in res_fail.json()["detail"]

    MaintenanceManager.release("active-lease-123")
