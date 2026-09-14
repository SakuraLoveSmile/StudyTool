#!/usr/bin/env python3
"""Create and validate offline RAG data backups without touching the source tree."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

MANIFEST = "MANIFEST.json"
EXCLUDED_PARTS = {"logs", "secrets", "backups", "__pycache__"}


def files_under(root: Path) -> list[Path]:
    result = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in EXCLUDED_PARTS or part.startswith(".") for part in rel.parts):
            continue
        if path.is_symlink():
            raise ValueError(f"symlink is not allowed in backup input: {rel}")
        if path.is_file():
            result.append(path)
    return result


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sqlite_check(path: Path) -> None:
    uri = f"file:{path}?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(f"SQLite integrity check failed for {path}: {integrity}")
        foreign = db.execute("PRAGMA foreign_key_check").fetchall()
        if foreign:
            raise ValueError(f"SQLite foreign key check failed for {path}: {foreign[:3]}")


def create(source: Path, output: Path, retain: int) -> Path:
    source = source.resolve()
    output = output.resolve()
    if not source.is_dir():
        raise ValueError(f"source is not a directory: {source}")
    if output == source or output.is_relative_to(source):
        raise ValueError("backup output must be outside the source data directory")
    output.mkdir(parents=True, exist_ok=True)
    output.chmod(0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = output / f"rag-{stamp}.tar.gz"
    if archive.exists():
        raise FileExistsError(archive)
    input_files = files_under(source)
    entries = []
    with tempfile.TemporaryDirectory(prefix="rag-manifest-") as temp:
        snapshot = Path(temp) / "data"
        snapshot.mkdir(mode=0o700)
        for path in input_files:
            target = snapshot / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            target.chmod(0o600)
        for path in snapshot.rglob("*"):
            if path.is_dir():
                path.chmod(0o700)
        for path in snapshot.rglob("*"):
            if path.is_file() and path.suffix in {".db", ".sqlite", ".sqlite3"}:
                sqlite_check(path)
        manifest = Path(temp) / MANIFEST
        for path in input_files:
            snap_path = snapshot / path.relative_to(source)
            entries.append({"path": str(path.relative_to(source)), "sha256": sha256(snap_path), "size": snap_path.stat().st_size})
        manifest.write_text(json.dumps({"version": 1, "files": entries}, indent=2, sort_keys=True) + "\n")
        temporary_archive = output / f".{archive.name}.tmp"
        with tarfile.open(temporary_archive, "w:gz") as tar:
            for path in snapshot.rglob("*"):
                if path.is_file():
                    tar.add(path, arcname=path.relative_to(snapshot), recursive=False)
            tar.add(manifest, arcname=MANIFEST, recursive=False)
        temporary_archive.chmod(0o600)
        temporary_archive.replace(archive)
    verified = sorted(output.glob("rag-*.tar.gz"), reverse=True)
    for old in verified[retain:]:
        old.unlink()
    return archive


def restore(archive: Path, destination: Path) -> None:
    archive = archive.resolve()
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError(f"restore destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="rag-restore-", dir=destination.parent) as temp:
        staging = Path(temp) / "data"
        staging.mkdir()
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            seen = set()
            manifest_members = [member for member in members if member.name == MANIFEST]
            if len(manifest_members) != 1:
                raise ValueError("archive must contain exactly one manifest")
            for member in members:
                name = Path(member.name)
                if member.name == MANIFEST:
                    continue
                if name.is_absolute() or ".." in name.parts or member.issym() or member.islnk() or not member.isfile():
                    raise ValueError(f"unsafe archive member: {member.name}")
                if member.name in seen or any(part in EXCLUDED_PARTS or part.startswith(".") for part in name.parts):
                    raise ValueError(f"invalid archive member: {member.name}")
                seen.add(member.name)
            manifest_member = tar.getmember(MANIFEST)
            if not manifest_member.isfile():
                raise ValueError("manifest must be a regular file")
            manifest = json.load(tar.extractfile(manifest_member))
            if manifest.get("version") != 1 or not isinstance(manifest.get("files"), list):
                raise ValueError("unsupported or invalid manifest")
            if any(not isinstance(entry, dict) or not isinstance(entry.get("path"), str) for entry in manifest["files"]):
                raise ValueError("invalid manifest entry")
            expected = {entry["path"]: entry for entry in manifest["files"]}
            if len(expected) != len(manifest["files"]):
                raise ValueError("manifest contains duplicate paths")
            if set(expected) != seen:
                raise ValueError("manifest does not match archive members")
            tar.extractall(staging)
        sqlite_files = []
        for rel, entry in expected.items():
            path = staging / rel
            if path.is_symlink() or not path.is_file() or path.stat().st_size != entry["size"] or sha256(path) != entry["sha256"]:
                raise ValueError(f"backup hash validation failed: {rel}")
            if path.suffix in {".db", ".sqlite", ".sqlite3"}:
                sqlite_files.append(path)
        for path in sqlite_files:
            sqlite_check(path)
        staging.rename(destination)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    create_parser = sub.add_parser("create")
    create_parser.add_argument("--source", type=Path, required=True)
    create_parser.add_argument("--output", type=Path, required=True)
    create_parser.add_argument("--retain", type=int, default=7)
    restore_parser = sub.add_parser("restore")
    restore_parser.add_argument("--archive", type=Path, required=True)
    restore_parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "create":
            print(create(args.source, args.output, max(1, args.retain)))
        else:
            restore(args.archive, args.destination)
            print(args.destination.resolve())
        return 0
    except (OSError, ValueError, tarfile.TarError, sqlite3.Error, KeyError, json.JSONDecodeError) as exc:
        print(f"backup error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
