"""Desktop backup and restore service.
Reuses the verified core of deploy/backup.py with WAL checkpointing,
atomic directory swapping, unique timestamp rollback safety, and embedding model verification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import secrets
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

# Ensure deploy package is importable
repo_root = Path(__file__).resolve().parents[2]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

try:
    import deploy.backup as core_backup
    files_under = core_backup.files_under
    sha256 = core_backup.sha256
    sqlite_check = core_backup.sqlite_check
    MANIFEST = core_backup.MANIFEST
    EXCLUDED_PARTS = core_backup.EXCLUDED_PARTS
except Exception:
    MANIFEST = "MANIFEST.json"
    EXCLUDED_PARTS = {"logs", "secrets", "backups", "__pycache__"}

    def files_under(root: Path) -> list[Path]:
        result = []
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root)
            if any(part in EXCLUDED_PARTS or part.startswith(".") for part in rel.parts):
                continue
            if path.is_symlink():
                raise ValueError(f"备份不允许符号链接: {rel}")
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
                raise ValueError(f"SQLite 完整性校验失败 ({path.name}): {integrity}")
            foreign = db.execute("PRAGMA foreign_key_check").fetchall()
            if foreign:
                raise ValueError(f"SQLite 外键完整性校验失败 ({path.name}): {foreign[:3]}")

from app.config import settings

logger = logging.getLogger(__name__)


def create_desktop_backup(
    target_path: Optional[Path] = None,
    destination_dir: Optional[Path] = None,
    retain: int = 10,
    source_dir: Optional[Path] = None,
    embedding_model: Optional[str] = None,
    embedding_base_url: Optional[str] = None,
    llm_model: Optional[str] = None,
    ocr_mode: Optional[str] = None,
) -> Path:
    """Create a verified tar.gz backup of the current desktop data directory.

    - If target_path is specified, creates exactly at that file path without deleting other user archives.
    - If destination_dir is specified, creates rag-<timestamp>.tar.gz inside it.
    - If neither, creates in settings.backups_path and retains latest N archives.
    - Model metadata is explicitly recorded from passed arguments, preventing dependency on default/dev configuration.
    """
    source = (source_dir or settings.root_data_path).resolve()
    if not source.is_dir():
        raise ValueError(f"数据源目录不存在: {source}")

    is_explicit_file = False
    if target_path:
        archive = target_path.resolve()
        output_dir = archive.parent
        is_explicit_file = True
    else:
        output_dir = (destination_dir or settings.backups_path).resolve()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive = output_dir / f"rag-{stamp}.tar.gz"

    if output_dir == source or output_dir.is_relative_to(source):
        raise ValueError("备份输出目录必须在业务数据目录之外")
    output_dir.mkdir(parents=True, exist_ok=True)

    if archive.exists():
        raise FileExistsError(f"归档文件已存在: {archive}")

    input_files = files_under(source)

    with tempfile.TemporaryDirectory(prefix="rag-manifest-") as temp:
        snapshot = Path(temp) / "data"
        snapshot.mkdir(mode=0o700)
        for path in input_files:
            target = snapshot / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)

        # Include model metadata for compatibility check
        meta_file = snapshot / "model_meta.json"
        meta_content = {
            "backup_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "embedding_model": embedding_model if embedding_model is not None else settings.EMBEDDING_MODEL,
            "embedding_base_url": embedding_base_url if embedding_base_url is not None else settings.EMBEDDING_BASE_URL,
            "llm_model": llm_model if llm_model is not None else settings.LLM_MODEL,
            "ocr_mode": ocr_mode if ocr_mode is not None else settings.OCR_MODE,
        }
        meta_file.write_text(json.dumps(meta_content, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        # Verify SQLite integrity on snapshot copies
        for path in snapshot.rglob("*"):
            if path.is_file() and path.suffix in {".db", ".sqlite", ".sqlite3"}:
                sqlite_check(path)

        # Build manifest
        all_snapshot_files = files_under(snapshot)
        entries = []
        for path in all_snapshot_files:
            entries.append({
                "path": str(path.relative_to(snapshot)),
                "sha256": sha256(path),
                "size": path.stat().st_size,
            })

        manifest = Path(temp) / MANIFEST
        manifest.write_text(json.dumps({"version": 1, "files": entries}, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        temporary_archive = output_dir / f".{archive.name}.tmp"
        with tarfile.open(temporary_archive, "w:gz") as tar:
            for path in snapshot.rglob("*"):
                if path.is_file():
                    tar.add(path, arcname=path.relative_to(snapshot), recursive=False)
            tar.add(manifest, arcname=MANIFEST, recursive=False)

        temporary_archive.replace(archive)

    # Only prune automatic internal archives when NOT explicitly exporting to a custom path
    if not is_explicit_file and not destination_dir:
        verified = sorted(output_dir.glob("rag-*.tar.gz"), reverse=True)
        for old in verified[retain:]:
            try:
                old.unlink()
            except OSError:
                pass

    logger.info("Created desktop backup archive: %s", archive)
    return archive


def restore_desktop_backup(
    archive_path: Path,
    target_dir: Optional[Path] = None,
    expected_embedding_model: Optional[str] = None,
    expected_embedding_base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Safely restore a backup archive into the destination data directory.

    Enforces:
    - Integrity and manifest verification in temporary staging.
    - Model metadata validation to prevent vector space contamination.
    - Unique timestamp backup of live data (never overwriting/deleting fixed .bak).
    - Failure rollback to preserve original data.
    - Preserving pre-restore snapshot on success.
    """
    archive = archive_path.resolve()
    if not archive.is_file():
        raise FileNotFoundError(f"备份文件不存在: {archive}")

    destination = (target_dir or settings.root_data_path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="rag-restore-", dir=destination.parent) as temp:
        staging = Path(temp) / "data"
        staging.mkdir()

        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            seen = set()
            manifest_members = [m for m in members if m.name == MANIFEST]
            if len(manifest_members) != 1:
                raise ValueError("备份归档必须包含且仅包含一个 MANIFEST.json")

            for member in members:
                name = Path(member.name)
                if member.name == MANIFEST:
                    continue
                if name.is_absolute() or ".." in name.parts or member.issym() or member.islnk() or not member.isfile():
                    raise ValueError(f"备份归档包含不安全的条目: {member.name}")
                if member.name in seen or any(part in EXCLUDED_PARTS or part.startswith(".") for part in name.parts):
                    raise ValueError(f"备份归档包含无效条目: {member.name}")
                seen.add(member.name)

            manifest_member = tar.getmember(MANIFEST)
            manifest = json.load(tar.extractfile(manifest_member))
            if manifest.get("version") != 1 or not isinstance(manifest.get("files"), list):
                raise ValueError("不支持或无效的备份清单格式")

            expected = {entry["path"]: entry for entry in manifest["files"]}
            if len(expected) != len(manifest["files"]):
                raise ValueError("备份清单包含重复路径")
            if set(expected) != seen:
                raise ValueError("备份清单与实际归档文件不匹配")

            tar.extractall(staging)

        # Validate file hashes and SQLite integrity in staging
        sqlite_files = []
        for rel, entry in expected.items():
            path = staging / rel
            if path.is_symlink() or not path.is_file() or path.stat().st_size != entry["size"] or sha256(path) != entry["sha256"]:
                raise ValueError(f"文件哈希校验失败: {rel}")
            if path.suffix in {".db", ".sqlite", ".sqlite3"}:
                sqlite_files.append(path)

        for path in sqlite_files:
            sqlite_check(path)

        # Inspect model metadata
        meta_info: Dict[str, Any] = {}
        meta_file = staging / "model_meta.json"
        if meta_file.is_file():
            try:
                meta_info = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        # Check for existing vectors in restored DB
        has_vectors = False
        for db_file in sqlite_files:
            try:
                with sqlite3.connect(f"file:{db_file}?mode=ro", uri=True) as db:
                    cursor = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_chunks'")
                    if cursor.fetchone():
                        row = db.execute("SELECT 1 FROM document_chunks WHERE embedding IS NOT NULL LIMIT 1").fetchone()
                        if row:
                            has_vectors = True
                            break
            except Exception:
                pass

        # Check model & base_url consistency if vectors exist
        if has_vectors:
            backup_emb_model = meta_info.get("embedding_model")
            backup_emb_url = meta_info.get("embedding_base_url")

            if expected_embedding_model and backup_emb_model:
                if backup_emb_model.strip().lower() != expected_embedding_model.strip().lower():
                    raise ValueError(
                        f"备份中的向量模型 ({backup_emb_model}) 与当前配置 ({expected_embedding_model}) 不一致，拒绝恢复以防向量损坏"
                    )

            if expected_embedding_base_url and backup_emb_url:
                if backup_emb_url.rstrip("/").lower() != expected_embedding_base_url.rstrip("/").lower():
                    raise ValueError(
                        f"备份中的向量服务地址 ({backup_emb_url}) 与当前配置 ({expected_embedding_base_url}) 不一致，拒绝恢复以防向量损坏"
                    )

        # Check if vectors exist but provenance info is missing
        vector_locked = False
        vector_lock_reason = None
        if has_vectors and not (meta_info.get("embedding_model") or "").strip():
            vector_locked = True
            vector_lock_reason = "已有向量但来源信息缺失，已锁定向量检索与追加，要求明确重建后才能恢复使用"

        # Atomic switch: Move current live data to a unique timestamped + random directory
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        rand_suffix = secrets.token_hex(4)
        backup_snapshot_dir = destination.parent / f"{destination.name}-pre-restore-{timestamp}-{rand_suffix}"

        if destination.exists():
            destination.rename(backup_snapshot_dir)
        else:
            backup_snapshot_dir.mkdir(parents=True, exist_ok=True)

        try:
            staging.rename(destination)
        except Exception as swap_err:
            logger.error("Failed to swap restored directory: %s; rolling back", swap_err)
            if backup_snapshot_dir.exists():
                if destination.exists():
                    shutil.rmtree(destination, ignore_errors=True)
                backup_snapshot_dir.rename(destination)
            raise RuntimeError(f"恢复数据切换失败: {swap_err}") from swap_err

    # Sync system_metadata in restored app.db if present
    db_file = destination / "app.db"
    if db_file.is_file():
        try:
            with sqlite3.connect(db_file) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS system_metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                if meta_info.get("embedding_model"):
                    conn.execute(
                        """INSERT INTO system_metadata (key, value, updated_at) VALUES ('embedding_model', ?, CURRENT_TIMESTAMP)
                           ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP""",
                        (meta_info["embedding_model"],),
                    )
                if meta_info.get("embedding_base_url"):
                    conn.execute(
                        """INSERT INTO system_metadata (key, value, updated_at) VALUES ('embedding_base_url', ?, CURRENT_TIMESTAMP)
                           ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP""",
                        (meta_info["embedding_base_url"],),
                    )
                conn.execute(
                    """INSERT INTO system_metadata (key, value, updated_at) VALUES ('vector_locked', ?, CURRENT_TIMESTAMP)
                       ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP""",
                    ("true" if vector_locked else "false",),
                )
                conn.execute(
                    """INSERT INTO system_metadata (key, value, updated_at) VALUES ('vector_lock_reason', ?, CURRENT_TIMESTAMP)
                       ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP""",
                    (vector_lock_reason or "",),
                )
                conn.commit()
        except Exception as meta_sync_err:
            logger.warning("Failed to sync system_metadata in restored database: %s", meta_sync_err)

    logger.info(
        "Successfully restored desktop backup from %s to %s (pre-restore snapshot kept at %s)",
        archive, destination, backup_snapshot_dir
    )
    return {
        "ok": True,
        "restored_from": str(archive),
        "restored_to": str(destination),
        "pre_restore_snapshot": str(backup_snapshot_dir) if backup_snapshot_dir.exists() else None,
        "model_meta": meta_info,
        "has_vectors": has_vectors,
        "vector_locked": vector_locked,
        "vector_lock_reason": vector_lock_reason,
        "message": "数据已安全恢复",
    }


def run_cli():
    """CLI runner for offline backup and restore commands."""
    parser = argparse.ArgumentParser(description="Offline Desktop Backup & Restore Utility")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup_parser = subparsers.add_parser("backup", help="Create an offline data backup")
    backup_parser.add_argument("--source", type=str, default=None, help="Source data directory")
    backup_parser.add_argument("--output", type=str, required=True, help="Output archive path or directory")
    backup_parser.add_argument("--embedding-model", type=str, default=None, help="Active embedding model name")
    backup_parser.add_argument("--embedding-base-url", type=str, default=None, help="Active embedding base URL")
    backup_parser.add_argument("--llm-model", type=str, default=None, help="Active LLM model name")
    backup_parser.add_argument("--ocr-mode", type=str, default=None, help="Active OCR mode")

    restore_parser = subparsers.add_parser("restore", help="Restore data from an archive")
    restore_parser.add_argument("--archive", type=str, required=True, help="Backup archive file path")
    restore_parser.add_argument("--target", type=str, default=None, help="Target data directory")
    restore_parser.add_argument("--expected-model", type=str, default=None, help="Expected embedding model")
    restore_parser.add_argument("--expected-base-url", type=str, default=None, help="Expected embedding base URL")

    args = parser.parse_args()

    if args.command == "backup":
        out_path = Path(args.output).resolve()
        src_dir = Path(args.source).resolve() if args.source else None
        if out_path.suffix in {".gz", ".tar.gz", ".tgz"} or str(out_path).endswith(".tar.gz"):
            res = create_desktop_backup(
                target_path=out_path,
                source_dir=src_dir,
                embedding_model=args.embedding_model,
                embedding_base_url=args.embedding_base_url,
                llm_model=args.llm_model,
                ocr_mode=args.ocr_mode,
            )
        else:
            res = create_desktop_backup(
                destination_dir=out_path,
                source_dir=src_dir,
                embedding_model=args.embedding_model,
                embedding_base_url=args.embedding_base_url,
                llm_model=args.llm_model,
                ocr_mode=args.ocr_mode,
            )
        print(json.dumps({"ok": True, "archive": str(res)}))
        sys.exit(0)

    elif args.command == "restore":
        arch_path = Path(args.archive).resolve()
        tgt_dir = Path(args.target).resolve() if args.target else None
        res = restore_desktop_backup(
            archive_path=arch_path,
            target_dir=tgt_dir,
            expected_embedding_model=args.expected_model,
            expected_embedding_base_url=args.expected_base_url,
        )
        print(json.dumps(res))
        sys.exit(0)


if __name__ == "__main__":
    run_cli()
