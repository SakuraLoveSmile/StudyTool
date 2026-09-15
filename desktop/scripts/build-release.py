#!/usr/bin/env python3
"""Automated release pipeline for RAG Windows Desktop Application.

Stages:
1. Verify environment & dependencies.
2. Validate the pinned feedback widget build configuration (fail fast, before any build).
3. Clean previous build directories to guarantee no stale assets.
4. Fresh frontend compilation (npm run build) with that configuration injected.
5. Verify the injected configuration actually reached the built frontend JS bundle.
6. Build Python 3.12 backend onedir via PyInstaller.
7. Compile desktop TypeScript & run desktop unit tests.
8. Package NSIS installer via electron-builder.
9. Compute SHA-256 digests and output release manifest.
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
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path


def configure_console_encoding() -> None:
    """Keep release logs printable on Windows runners with legacy code pages."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def run_command(
    cmd: list[str],
    cwd: Path,
    description: str,
    env: dict[str, str] | None = None,
) -> None:
    print(f"\n[Release Pipeline] >>> {description}...")
    print(f"Executing: {' '.join(cmd)} in {cwd}")
    subprocess.check_call(cmd, cwd=cwd, env=env)


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


# ---------------------------------------------------------------------------
# Feedback widget build configuration (@feedback/web 0.2.0)
# ---------------------------------------------------------------------------
#
# 前端只在 BOTH `VITE_FEEDBACK_API_BASE` 与 `VITE_FEEDBACK_APP_ID` 都非空时才挂载反馈入口
# （见 frontend/src/composables/useFeedbackWidget.ts 与 frontend/src/App.vue）。
# 开发者机器上这两个值来自被 git 忽略的 frontend/.env.local；发行构建**绝不能**依赖它，
# 必须由本脚本显式注入下面两个常量。
#
# 失败策略：任一值为空、API base 不是合法的绝对 https 地址（无 query / fragment / 结尾斜杠）、
# 或与发行常量不一致，一律抛出 FeedbackConfigError 让构建非零退出。
# 绝不回退到空值，也绝不回退到任何本地 / 开发 / 预发地址。

FEEDBACK_API_BASE_ENV = "VITE_FEEDBACK_API_BASE"
FEEDBACK_APP_ID_ENV = "VITE_FEEDBACK_APP_ID"

#: 发行版唯一允许的反馈服务 origin（punycode，无结尾斜杠）。
FEEDBACK_API_BASE = "https://feedback.xn--fhqths51enha.cn"
#: 发行版唯一允许的 appId。
FEEDBACK_APP_ID = "rag"

#: 产物文本门禁里 appId 字面量允许的引号写法。
#: 实测（Vite 8 / esbuild）产物为反引号形式：`r.apiBase=\`https://...\`,r.appId=\`rag\``，
#: 因此必须接受双引号、单引号与反引号三种写法，否则真实发布构建会被门禁误判为失败。
FEEDBACK_BUNDLE_APP_ID_QUOTES = ('"', "'", "`")


class FeedbackConfigError(RuntimeError):
    """反馈组件构建期配置校验失败（沿用本脚本既有的硬门禁风格，直接中断构建）。"""


def validate_feedback_config(
    api_base: str | None,
    app_id: str | None,
    *,
    expected_api_base: str | None = None,
) -> None:
    """校验反馈组件构建期配置，不合规即抛 :class:`FeedbackConfigError`。

    **纯函数、零副作用**：不读环境变量、不碰文件系统、不在导入时执行任何构建动作，
    因此可以直接单元测试。本文件名带连字符，导入时用 importlib：

        spec = importlib.util.spec_from_file_location(
            "build_release", "desktop/scripts/build-release.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)   # 只定义函数与常量，不会触发构建
        mod.validate_feedback_config(mod.FEEDBACK_API_BASE, mod.FEEDBACK_APP_ID)

    规则：
      - `api_base` / `app_id` 必须存在且去除首尾空白后非空；
      - `api_base` 必须是绝对的 `https://` URL，且不含 query、fragment 与结尾斜杠；
      - `api_base` 不得含空白字符；
      - `app_id` 必须恰好等于 `FEEDBACK_APP_ID`（`"rag"`）；
      - 当传入 `expected_api_base` 时（发行构建路径必须传入 `FEEDBACK_API_BASE`），
        `api_base` 还必须与之完全相等——语法合法但指向预发 / 本地服务的地址同样判定失败。
    """
    if api_base is None or not api_base.strip():
        raise FeedbackConfigError(
            f"反馈组件配置校验失败：{FEEDBACK_API_BASE_ENV} 缺失或为空。"
            f"Windows 发行构建必须显式注入 {FEEDBACK_API_BASE}；禁止回退到空值或本地/开发地址。"
        )
    if app_id is None or not app_id.strip():
        raise FeedbackConfigError(
            f"反馈组件配置校验失败：{FEEDBACK_APP_ID_ENV} 缺失或为空。"
            f"Windows 发行构建必须显式注入 {FEEDBACK_APP_ID!r}；禁止回退到空值或开发标识。"
        )

    base = api_base.strip()
    resolved_app_id = app_id.strip()

    if any(ch.isspace() for ch in base):
        raise FeedbackConfigError(
            f"反馈组件配置校验失败：{FEEDBACK_API_BASE_ENV} 含空白字符 {base!r}。"
        )

    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme != "https":
        raise FeedbackConfigError(
            f"反馈组件配置校验失败：{FEEDBACK_API_BASE_ENV} 必须是绝对的 https:// 地址，"
            f"当前为 {base!r}（http:// 或相对地址一律拒绝）。"
        )
    if not parsed.netloc:
        raise FeedbackConfigError(
            f"反馈组件配置校验失败：{FEEDBACK_API_BASE_ENV} 缺少主机名，当前为 {base!r}。"
        )
    if parsed.query or parsed.fragment:
        raise FeedbackConfigError(
            f"反馈组件配置校验失败：{FEEDBACK_API_BASE_ENV} 不得包含 query 或 fragment，当前为 {base!r}。"
        )
    if base.endswith("/"):
        raise FeedbackConfigError(
            f"反馈组件配置校验失败：{FEEDBACK_API_BASE_ENV} 不得以斜杠结尾，当前为 {base!r}。"
        )
    if expected_api_base is not None and base != expected_api_base:
        raise FeedbackConfigError(
            f"反馈组件配置校验失败：{FEEDBACK_API_BASE_ENV}={base!r} 与发行常量 "
            f"{expected_api_base!r} 不一致。发行包只允许指向正式反馈服务。"
        )
    if resolved_app_id != FEEDBACK_APP_ID:
        raise FeedbackConfigError(
            f"反馈组件配置校验失败：{FEEDBACK_APP_ID_ENV}={resolved_app_id!r}，"
            f"发行版要求恰好为 {FEEDBACK_APP_ID!r}。"
        )


def feedback_build_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """返回注入了发行版反馈组件配置的前端构建环境变量（纯函数，可单测）。

    - 发行值只来自本模块常量，**不读取** frontend/.env.local（那是开发者机器的本地文件）；
      进程环境变量在 Vite 的 `loadEnv` 中优先于 .env / .env.local，因此这里的显式注入生效；
    - 若传入的环境中已存在同名变量且与发行常量不一致，判定为配置漂移并立即失败
      （既不静默覆盖，也不静默沿用）；
    - 返回前调用 :func:`validate_feedback_config` 自校验（含 `expected_api_base` 硬钉），
      保证绝不带病进入构建。
    """
    env = dict(os.environ if base_env is None else base_env)
    for name, expected in (
        (FEEDBACK_API_BASE_ENV, FEEDBACK_API_BASE),
        (FEEDBACK_APP_ID_ENV, FEEDBACK_APP_ID),
    ):
        existing = (env.get(name) or "").strip()
        if existing and existing != expected:
            raise FeedbackConfigError(
                f"反馈组件配置校验失败：环境变量 {name}={existing!r} 与发行常量 {expected!r} 不一致。"
                f"发行构建禁止使用本地 / 开发 / 预发地址；请移除该变量或将其改为发行常量。"
            )

    env[FEEDBACK_API_BASE_ENV] = FEEDBACK_API_BASE
    env[FEEDBACK_APP_ID_ENV] = FEEDBACK_APP_ID
    validate_feedback_config(
        env[FEEDBACK_API_BASE_ENV],
        env[FEEDBACK_APP_ID_ENV],
        expected_api_base=FEEDBACK_API_BASE,
    )
    return env


def verify_feedback_bundle_contains_config(
    dist_dir: Path,
    api_base: str = FEEDBACK_API_BASE,
    app_id: str = FEEDBACK_APP_ID,
) -> list[Path]:
    """构建后文本门禁：确认注入的配置确实被内联进了前端产物 JS。

    ⚠️ 这个文本门禁**只能证明"配置注入成功"**，它**不能**证明反馈组件真的挂载成功。
    "组件是否挂载"还取决于运行时会话状态（登录完成、允许展示外壳）与组件包能否加载，
    真正的挂载验证必须在打包后的应用里启动观察（本仓库 CI 不做，也不得宣称已做）。
    请勿把本门禁当成挂载测试，也不要据此宣称 Windows 安装包已验证挂载。

    判别方式：在 `frontend/dist/assets` 下所有 `.js` 产物中查找 API base 的 host 字面量
    （`feedback.xn--fhqths51enha.cn`），并要求**同一个产物文件**里同时出现带引号的 appId
    字面量。appId `"rag"` 是短字面量，单独匹配判别力弱，因此以 host 字面量为主要证据。

    返回命中配置的产物文件列表；任一条件不满足则抛出 :class:`FeedbackConfigError`。
    """
    assets_dir = dist_dir / "assets"
    js_files = sorted(p for p in assets_dir.rglob("*.js") if p.is_file())
    if not js_files:
        raise FeedbackConfigError(
            f"反馈组件产物门禁失败：{assets_dir} 下没有任何 .js 产物，无法证明配置已注入。"
        )

    host_marker = urllib.parse.urlsplit(api_base).netloc or api_base
    app_id_markers = [f"{quote}{app_id}{quote}" for quote in FEEDBACK_BUNDLE_APP_ID_QUOTES]
    # Word-boundary fallback: a minifier may inline the value without surrounding quotes
    # (e.g. `appId=rag`), and `rag` must still not match inside `storage` / `fragment`.
    app_id_word = re.compile(rf"(?<![A-Za-z0-9_-]){re.escape(app_id)}(?![A-Za-z0-9_-])")

    host_hits: list[Path] = []
    matched: list[Path] = []
    for js_file in js_files:
        text = js_file.read_text(encoding="utf-8", errors="replace")
        if host_marker not in text:
            continue
        host_hits.append(js_file)
        if any(marker in text for marker in app_id_markers) or app_id_word.search(text):
            matched.append(js_file)

    if not host_hits:
        raise FeedbackConfigError(
            f"反馈组件产物门禁失败：已扫描 {len(js_files)} 个产物 JS，均未出现 API base 字面量 "
            f"{host_marker!r}。说明构建期 {FEEDBACK_API_BASE_ENV} 没有真正注入"
            f"（检查是否被 frontend/.env.local、缓存或其它 VITE_* 覆盖）。"
        )
    if not matched:
        raise FeedbackConfigError(
            f"反馈组件产物门禁失败：出现 API base 的产物 "
            f"({', '.join(p.name for p in host_hits)}) 中没有带引号的 appId 字面量 "
            f"{app_id!r}，说明构建期 {FEEDBACK_APP_ID_ENV} 没有真正注入。"
        )
    return matched


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

    # 0b. Hard-gate the feedback widget configuration before any expensive step runs.
    #     A misconfigured release must die here in seconds, not after packaging.
    print("\n[Step 0b] Validating feedback widget build configuration...")
    frontend_build_env = feedback_build_env()
    print(f"  {FEEDBACK_API_BASE_ENV} = {frontend_build_env[FEEDBACK_API_BASE_ENV]}")
    print(f"  {FEEDBACK_APP_ID_ENV} = {frontend_build_env[FEEDBACK_APP_ID_ENV]}")
    print("  Source: build-release.py constants (frontend/.env.local is never read).")

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

    # 2. Build fresh frontend, explicitly injecting the pinned feedback configuration.
    #    Process env wins over frontend/.env.local inside Vite's loadEnv, so this injection
    #    is authoritative and does not depend on that git-ignored file existing.
    run_command(
        [npm_executable(), "run", "build"],
        frontend_dir,
        "Building frontend assets (Vite + vue-tsc)",
        env=frontend_build_env,
    )
    dist_html = frontend_dir / "dist" / "index.html"
    if not dist_html.is_file():
        raise RuntimeError("Frontend build failed: dist/index.html is missing!")

    # 2b. Build-time gate: the injected values must actually be present in the built JS.
    #     NOTE: this textual gate proves CONFIGURATION INJECTION ONLY. It does NOT prove the
    #     feedback widget is mounted. Whether the widget mounts still depends on runtime
    #     conditions (auth ready, shell visible, component module loadable), so real
    #     "widget is mounted" verification requires launching the packaged app and observing
    #     it. Do not mistake this gate for a mount test.
    injected_assets = verify_feedback_bundle_contains_config(frontend_dir / "dist")
    print(
        "  Feedback config verified in bundle: "
        + ", ".join(p.name for p in injected_assets)
    )

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
