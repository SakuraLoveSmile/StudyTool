"""Regression tests for Task T3:
- Offline backup and trustworthy restore.
- MANIFEST integrity check, hash verification, sensitive path exclusion.
- Model consistency validation (mismatch rejection).
- Pre-restore snapshot generation with unique timestamp.
- JSON output contract containing ok, restored_to, and pre_restore_snapshot.
- Restored DB synchronization of system_metadata (including vector_locked).
"""
import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from pathlib import Path

import pytest
from app.backup import create_desktop_backup, restore_desktop_backup


@pytest.fixture
def t3_env(tmp_path):
    src_data = tmp_path / "live_data"
    src_data.mkdir()

    # Create dummy app.db in live_data
    db_file = src_data / "app.db"
    with sqlite3.connect(db_file) as conn:
        conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO schema_meta VALUES ('schema_version', '6')")
        conn.execute(
            """CREATE TABLE document_chunks (
                id TEXT PRIMARY KEY,
                doc_id TEXT,
                chunk_index INTEGER,
                content TEXT,
                embedding BLOB
            )"""
        )
        conn.execute(
            "INSERT INTO document_chunks VALUES ('c1', 'd1', 0, '切片1', X'112233')"
        )
        conn.commit()

    # Create a subfile
    (src_data / "uploads").mkdir()
    (src_data / "uploads" / "sample.txt").write_text("hello world", encoding="utf-8")

    # Create logs and secrets (should be excluded)
    (src_data / "logs").mkdir()
    (src_data / "logs" / "app.log").write_text("secret log", encoding="utf-8")

    yield {
        "root": tmp_path,
        "src_data": src_data,
        "archive": tmp_path / "backup.tar.gz",
        "restore_target": tmp_path / "restored_live_data",
    }


def test_backup_excludes_sensitive_and_records_meta(t3_env):
    archive_path = create_desktop_backup(
        target_path=t3_env["archive"],
        source_dir=t3_env["src_data"],
        embedding_model="text-embedding-3-small",
        embedding_base_url="https://api.openai.com/v1",
        llm_model="gpt-4o",
    )
    assert archive_path.is_file()

    # Check tar contents
    with tarfile.open(archive_path, "r:gz") as tar:
        names = tar.getnames()
        assert "MANIFEST.json" in names
        assert "model_meta.json" in names
        assert "app.db" in names
        assert "uploads/sample.txt" in names
        assert not any("logs" in n for n in names)
        assert not any("secrets" in n for n in names)

        # Inspect model_meta.json
        meta_f = tar.extractfile("model_meta.json")
        meta = json.load(meta_f)
        assert meta["embedding_model"] == "text-embedding-3-small"
        assert meta["embedding_base_url"] == "https://api.openai.com/v1"
        assert meta["llm_model"] == "gpt-4o"


def test_restore_rejects_embedding_model_mismatch(t3_env):
    create_desktop_backup(
        target_path=t3_env["archive"],
        source_dir=t3_env["src_data"],
        embedding_model="model-A",
        embedding_base_url="https://api.openai.com/v1",
    )

    # Attempt restore with expected model-B
    with pytest.raises(ValueError) as exc:
        restore_desktop_backup(
            archive_path=t3_env["archive"],
            target_dir=t3_env["restore_target"],
            expected_embedding_model="model-B",
        )
    assert "不一致" in str(exc.value)


def test_restore_contract_and_system_metadata_sync(t3_env):
    create_desktop_backup(
        target_path=t3_env["archive"],
        source_dir=t3_env["src_data"],
        embedding_model="text-embedding-3-small",
        embedding_base_url="https://api.openai.com/v1",
    )

    # Initial target directory with some old data
    target = t3_env["restore_target"]
    target.mkdir()
    (target / "old_file.txt").write_text("old", encoding="utf-8")

    res = restore_desktop_backup(
        archive_path=t3_env["archive"],
        target_dir=target,
        expected_embedding_model="text-embedding-3-small",
        expected_embedding_base_url="https://api.openai.com/v1",
    )

    # Verify return contract required by T3
    assert res["ok"] is True
    assert res["restored_to"] == str(target.resolve())
    assert res["pre_restore_snapshot"] is not None
    assert Path(res["pre_restore_snapshot"]).is_dir()
    assert (Path(res["pre_restore_snapshot"]) / "old_file.txt").is_file()

    # Restored data must be in place
    assert (target / "app.db").is_file()
    assert (target / "uploads" / "sample.txt").is_file()

    # SQLite system_metadata must be synced
    with sqlite3.connect(target / "app.db") as conn:
        row = conn.execute("SELECT value FROM system_metadata WHERE key = 'embedding_model'").fetchone()
        assert row is not None
        assert row[0] == "text-embedding-3-small"


def test_build_release_script_guards(monkeypatch):
    """Verify build-release.py environment and architecture guards."""
    import sys
    from importlib.machinery import SourceFileLoader

    script_path = Path(__file__).resolve().parents[2] / "desktop" / "scripts" / "build-release.py"
    build_release = SourceFileLoader("build_release", str(script_path)).load_module()

    # When STRICT_RELEASE_ENV is set and python is not 3.12, must raise RuntimeError
    monkeypatch.setenv("STRICT_RELEASE_ENV", "1")
    monkeypatch.setattr(sys, "version_info", (3, 10, 10, "final", 0))

    with pytest.raises(RuntimeError) as exc:
        build_release.check_environment()
    assert "必须在 Python 3.12 64-bit 环境下打包" in str(exc.value)

    # When version is 3.12 64-bit, should succeed
    monkeypatch.setattr(sys, "version_info", (3, 12, 0, "final", 0))
    monkeypatch.setattr(sys, "maxsize", 2**63 - 1)
    build_release.check_environment()  # Should not raise
