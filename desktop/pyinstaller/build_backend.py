#!/usr/bin/env python3
"""Build the PyInstaller onedir distribution for the desktop backend."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

def main():
    script_dir = Path(__file__).resolve().parent
    desktop_dir = script_dir.parent
    repo_root = desktop_dir.parent
    frontend_dist = repo_root / "frontend" / "dist"

    print("=== Step 1: Ensure frontend is built ===")
    if not frontend_dist.is_dir() or not (frontend_dist / "index.html").is_file():
        print("Frontend dist not found, building frontend...")
        npm = "npm.cmd" if os.name == "nt" else "npm"
        subprocess.check_call([npm, "run", "build"], cwd=repo_root / "frontend")

    print("=== Step 2: Running PyInstaller ===")
    dist_dir = desktop_dir / "build"
    dist_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        f"--distpath={dist_dir}",
        str(script_dir / "backend.spec"),
    ]
    print(f"Running command: {' '.join(cmd)}")
    subprocess.check_call(cmd, cwd=script_dir)

    print("=== Backend onedir build complete! ===")
    print(f"Output directory: {dist_dir / 'backend'}")

if __name__ == "__main__":
    main()
