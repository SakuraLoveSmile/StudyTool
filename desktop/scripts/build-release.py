#!/usr/bin/env python3
"""Automated release pipeline for RAG Windows Desktop Application.

Stages:
1. Verify environment & dependencies.
2. Clean previous build directories to guarantee no stale assets.
3. Fresh frontend compilation (npm run build).
4. Build Python 3.12 backend onedir via PyInstaller.
5. Compile desktop TypeScript & run desktop unit tests.
6. Package NSIS installer via electron-builder.
7. Compute SHA-256 digests and output release manifest.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def configure_console_encoding() -> None:
    """Keep release logs printable on Windows runners with legacy code pages."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def run_command(cmd: list[str], cwd: Path, description: str) -> None:
    print(f"\n[Release Pipeline] >>> {description}...")
    print(f"Executing: {' '.join(cmd)} in {cwd}")
    subprocess.check_call(cmd, cwd=cwd)


def npm_executable() -> str:
    """Return the executable name that Python can launch on each host OS."""
    return "npm.cmd" if os.name == "nt" else "npm"


def npx_executable() -> str:
    """Return the executable name that Python can launch on each host OS."""
    return "npx.cmd" if os.name == "nt" else "npx"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_environment():
    """Verify build prerequisites: Python 3.12 64-bit and target platform constraints."""
    print("\n[Step 0] Verifying environment & toolchain...")
    is_64bit = sys.maxsize > 2**32
    py_ver = f"{sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}"
    print(f"  Host OS: {sys.platform} ({platform.machine()})")
    print(f"  Python Version: {py_ver} ({'64-bit' if is_64bit else '32-bit'})")

    if sys.platform == "win32" or os.environ.get("STRICT_RELEASE_ENV"):
        if sys.version_info[:2] != (3, 12):
            raise RuntimeError(
                f"环境校验失败：当前 Python 版本为 {py_ver}，Windows 发行版必须在 Python 3.12 64-bit 环境下打包！"
            )
        if not is_64bit:
            raise RuntimeError("环境校验失败：Windows 发行版必须在 64 位 Python 环境下打包！")
    else:
        if sys.version_info[:2] != (3, 12):
            print(f"  [Warning] 当前 Python 版本为 {py_ver}（生产 Windows 环境要求 Python 3.12 64-bit）")


def main():
    script_dir = Path(__file__).resolve().parent
    desktop_dir = script_dir.parent
    repo_root = desktop_dir.parent
    frontend_dir = repo_root / "frontend"
    pyinstaller_dir = desktop_dir / "pyinstaller"

    print("==================================================")
    print(" RAG Windows Desktop Release Pipeline ")
    print("==================================================")

    # 0. Check environment
    check_environment()

    # 1. Clean previous build artifacts
    print("\n[Step 1] Cleaning stale build outputs...")
    dirs_to_clean = [
        frontend_dir / "dist",
        desktop_dir / "build",
        desktop_dir / "dist",
        desktop_dir / "release",
        pyinstaller_dir / "build",
        pyinstaller_dir / "dist",
    ]
    for d in dirs_to_clean:
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            print(f"  Removed: {d}")

    # 2. Build fresh frontend
    run_command([npm_executable(), "run", "build"], frontend_dir, "Building frontend assets (Vite + vue-tsc)")
    dist_html = frontend_dir / "dist" / "index.html"
    if not dist_html.is_file():
        raise RuntimeError("Frontend build failed: dist/index.html is missing!")

    # 3. Build backend onedir with PyInstaller
    run_command(
        [sys.executable, str(pyinstaller_dir / "build_backend.py")],
        pyinstaller_dir,
        "Building backend onedir distribution with PyInstaller",
    )
    backend_out = desktop_dir / "build" / "backend"
    if not backend_out.is_dir():
        raise RuntimeError(f"PyInstaller build failed: {backend_out} does not exist!")

    if sys.platform == "win32" or os.environ.get("FORCE_BUILD_WIN"):
        run_command(
            [sys.executable, str(script_dir / "verify-windows-backend.py"), str(backend_out / "backend.exe")],
            desktop_dir,
            "Starting frozen backend and validating desktop readiness",
        )

    # 4. Compile desktop TypeScript
    run_command([npm_executable(), "run", "build"], desktop_dir, "Compiling desktop TypeScript")

    # 5. Run desktop unit tests
    run_command([npm_executable(), "test"], desktop_dir, "Running desktop unit tests")

    # 6. Package with electron-builder
    if sys.platform == "win32" or os.environ.get("FORCE_BUILD_WIN"):
        run_command(
            [npx_executable(), "electron-builder", "--win", "--x64", "--publish", "never"],
            desktop_dir,
            "Packaging Windows installer (NSIS)",
        )
    else:
        print("\n[Notice] 当前主机系统不是 Windows (为 " + sys.platform + ")。")
        print("[Notice] 非 Windows 环境绝不宣称生成 Windows 发行版，跳过 NSIS 安装包生成。")
        print("若需产出 Windows 生产安装包，请在干净的 Windows 10/11 x64 实机环境执行: python desktop/scripts/build-release.py")
        sys.exit(0)

    # 7. Hash output installers & check NSIS artifact
    release_dir = desktop_dir / "release"
    installers = list(release_dir.glob("*.exe"))
    if not installers:
        raise RuntimeError("构建错误：在 release 目录中未检测到生成的 NSIS .exe 安装包，构建判定失败！")
    update_manifests = list(release_dir.glob("latest*.yml"))
    blockmaps = list(release_dir.glob("*.blockmap"))
    if not update_manifests or not blockmaps:
        raise RuntimeError(
            "构建错误：缺少 electron-updater 所需的 latest*.yml 或 .blockmap，禁止发布不完整更新包"
        )
    package_version = json.loads((desktop_dir / "package.json").read_text(encoding="utf-8"))["version"]
    installer_names = {installer.name for installer in installers}
    for update_manifest in update_manifests:
        manifest_text = update_manifest.read_text(encoding="utf-8")
        if f"version: {package_version}" not in manifest_text:
            raise RuntimeError(f"构建错误：{update_manifest.name} 未声明当前版本 {package_version}")
        referenced_files = {
            match.group(1).strip().strip("'\"")
            for match in re.finditer(r"^\s*(?:url|path):\s*(.+?)\s*$", manifest_text, re.MULTILINE)
        }
        missing_files = sorted(referenced_files - installer_names)
        if missing_files:
            raise RuntimeError(
                f"构建错误：{update_manifest.name} 引用的安装包不存在: {', '.join(missing_files)}"
            )
    missing_blockmaps = [inst.name for inst in installers if not (release_dir / f"{inst.name}.blockmap").is_file()]
    if missing_blockmaps:
        raise RuntimeError(f"构建错误：安装包缺少对应 blockmap: {', '.join(missing_blockmaps)}")

    manifest = {
        "build_time": datetime.now(timezone.utc).isoformat(),
        "version": package_version,
        "installers": [],
        "update_manifests": [p.name for p in update_manifests],
        "blockmaps": [p.name for p in blockmaps],
    }
    sha256_lines = []

    for inst in installers:
        h = sha256_file(inst)
        manifest["installers"].append({
            "name": inst.name,
            "size": inst.stat().st_size,
            "sha256": h,
        })
        sha256_lines.append(f"{h}  {inst.name}")
        print(f"Generated Installer: {inst.name} ({inst.stat().st_size} bytes)")
        print(f"  SHA-256: {h}")

    (release_dir / "RELEASE_MANIFEST.json").write_bytes(
        json.dumps(manifest, indent=2).encode("utf-8")
    )
    (release_dir / "SHA256SUMS.txt").write_bytes(
        ("\n".join(sha256_lines) + "\n").encode("utf-8")
    )
    print("\n[Success] Release build complete! Artifacts in desktop/release/")


if __name__ == "__main__":
    configure_console_encoding()
    main()
