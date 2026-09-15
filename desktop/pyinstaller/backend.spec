# -*- mode: python ; coding: utf-8 -*-
import os
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

repo_root = Path(os.path.abspath("../..")).resolve()
backend_root = repo_root / "backend"
frontend_dist = repo_root / "frontend" / "dist"

# Enforce fresh frontend dist: fail immediately if missing!
if not frontend_dist.is_dir() or not (frontend_dist / "index.html").is_file():
    raise RuntimeError(f"前端构建产物缺失或不完整: {frontend_dist}。必须执行 npm run build 构建前端，严禁复用不明来源旧产物！")

datas = []
datas += collect_data_files('jieba')
datas.append((str(frontend_dist), 'frontend_dist'))
if (repo_root / 'deploy').is_dir():
    datas.append((str(repo_root / 'deploy'), 'deploy'))

hiddenimports = [
    'uvicorn',
    'uvicorn.logging',
    'uvicorn.loops',
    'uvicorn.loops.auto',
    'uvicorn.protocols',
    'uvicorn.protocols.http',
    'uvicorn.protocols.http.auto',
    'uvicorn.protocols.websockets',
    'uvicorn.protocols.websockets.auto',
    'uvicorn.lifespan',
    'uvicorn.lifespan.on',
    'fitz',
    'pymupdf',
    'numpy',
    'jieba',
    'sqlite3',
    'starlette.staticfiles',
    'starlette.middleware',
    'starlette.middleware.base',
    'fastapi',
    'pydantic',
    'pydantic_settings',
    'httpx',
    'app.main',
    'app.config',
    'app.desktop',
    'app.backup',
    'deploy.backup',
]
hiddenimports += collect_submodules('app.routers')
hiddenimports += collect_submodules('app.services')

a = Analysis(
    [str(backend_root / 'app' / 'main.py')],
    pathex=[str(backend_root), str(repo_root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pytest', 'tests', 'debugpy'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='backend',
)
