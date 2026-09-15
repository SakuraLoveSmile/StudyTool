#!/usr/bin/env python3
"""Smoke-test the frozen backend before an installer can be published."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def request_json(url: str, token: str, method: str = "GET", body: dict | None = None) -> dict:
    payload = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=payload, method=method)
    request.add_header("Host", url.split("//", 1)[1].split("/", 1)[0])
    request.add_header("X-RAG-Instance-Token", token)
    if payload is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=3) as response:
        raw = response.read().decode("utf-8")
    decoded = json.loads(raw) if raw else {}
    if not isinstance(decoded, dict):
        raise RuntimeError(f"unexpected response from {url}")
    return decoded


def tail(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[-6000:].strip()


def backend_log_tail(logs_dir: Path, limit: int = 15) -> str:
    """
    Renders the last structured backend records.

    This is the single most useful artifact when the frozen backend fails to start: the
    startup phase events say exactly how far it got. Omitting it once turned a one-line
    diagnosis into a guessing game, so every failure path prints it now.
    """
    path = logs_dir / "backend.jsonl"
    if not path.is_file():
        return f"(no backend log file at {path})"
    lines = [line for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
    if not lines:
        return f"(backend log is empty: {path})"
    rendered: list[str] = []
    for line in lines[-limit:]:
        try:
            entry = json.loads(line)
        except ValueError:
            rendered.append(f"  {line[:160]}")
            continue
        rendered.append(
            f"  {entry.get('timestamp', '')} {str(entry.get('level', '')):<5} "
            f"{str(entry.get('source', '')):<8} {entry.get('event', '')} "
            f"phase={entry.get('phase')} | {str(entry.get('message', ''))[:70]}"
        )
    return "\n".join(rendered)


def failure_report(logs_dir: Path, stdout_path: Path, stderr_path: Path) -> str:
    """Everything needed to diagnose a startup failure, in one printable block."""
    return (
        f"--- backend log tail ({logs_dir / 'backend.jsonl'}) ---\n{backend_log_tail(logs_dir)}\n"
        f"--- backend stdout tail ---\n{tail(stdout_path) or '(empty)'}\n"
        f"--- backend stderr tail ---\n{tail(stderr_path) or '(empty)'}"
    )


def main() -> int:
    if os.name != "nt":
        raise RuntimeError("verify-windows-backend.py must run on Windows")
    if len(sys.argv) != 2:
        raise SystemExit("usage: verify-windows-backend.py <backend.exe>")

    executable = Path(sys.argv[1]).resolve()
    if not executable.is_file():
        raise RuntimeError(f"backend executable not found: {executable}")

    port = free_port()
    token = uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="rag-backend-smoke-") as temp:
        root = Path(temp)
        logs = root / "logs"
        stdout_path = root / "backend.stdout.log"
        stderr_path = root / "backend.stderr.log"
        env = {
            **os.environ,
            "RAG_DESKTOP_MODE": "1",
            "RAG_DESKTOP_DATA_DIR": str(root / "appdata"),
            "LOG_DIR": str(logs),
        }
        proc: subprocess.Popen[str] | None = None
        try:
            with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
                "w", encoding="utf-8"
            ) as stderr:
                proc = subprocess.Popen(
                    [str(executable), "--desktop", f"--port={port}", "--host=127.0.0.1"],
                    cwd=str(executable.parent),
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=stdout,
                    stderr=stderr,
                    text=True,
                )
                assert proc.stdin is not None
                proc.stdin.write(json.dumps({"instance_token": token, "port": port, "host": "127.0.0.1"}) + "\n")
                proc.stdin.flush()

            base_url = f"http://127.0.0.1:{port}"
            # 90s, not 30s: a cold PyInstaller onedir start on a fresh Windows runner
            # (UPX-compressed exe, first-run AV scanning, heavy imports) can be slow,
            # and patience here must not be mistaken for a gate. The gate still requires
            # readiness, index.html serving and a clean shutdown exit code.
            deadline = time.monotonic() + 90
            status = None
            last_error = ""
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError(
                        f"backend exited with code {proc.returncode}\n"
                        + failure_report(logs, stdout_path, stderr_path)
                    )
                try:
                    status = request_json(f"{base_url}/api/desktop/status", token)
                    break
                except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
                    last_error = str(exc)
                    time.sleep(0.35)
            if status is None:
                raise RuntimeError(
                    f"backend readiness timeout: {last_error}\n"
                    + failure_report(logs, stdout_path, stderr_path)
                )
            if status.get("desktop_mode") is not True:
                raise RuntimeError(f"desktop status is not enabled: {status}")

            with urllib.request.urlopen(f"{base_url}/", timeout=3) as response:
                html = response.read(4096).decode("utf-8", errors="replace")
            if response.status != 200 or "<html" not in html.lower():
                raise RuntimeError("frontend index.html was not served by the frozen backend")

            lease_id = uuid.uuid4().hex
            request_json(
                f"{base_url}/api/desktop/maintenance/acquire",
                token,
                "POST",
                {"lease_id": lease_id, "operation": "release smoke test", "is_exit": True},
            )
            request_json(f"{base_url}/api/desktop/drain", token, "POST", {"lease_id": lease_id})
            request_json(f"{base_url}/api/desktop/shutdown", token, "POST", {"lease_id": lease_id})
            if proc.wait(timeout=15) != 0:
                raise RuntimeError(
                    f"backend shutdown returned {proc.returncode}\n"
                    + failure_report(logs, stdout_path, stderr_path)
                )
            print(f"Windows backend smoke test passed on port {port}")
            return 0
        finally:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            if proc is not None and proc.returncode not in (None, 0):
                print(failure_report(logs, stdout_path, stderr_path), file=sys.stderr)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        raise SystemExit(f"Windows backend smoke test failed: {exc}")
