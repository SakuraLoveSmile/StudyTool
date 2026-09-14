"""Release archive tests use synthetic data only, including a live WAL fixture."""
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tarfile

import pytest

spec = importlib.util.spec_from_file_location('release_backup', Path(__file__).resolve().parents[2] / 'deploy/backup.py')
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


def test_wal_snapshot_restore_and_no_overwrite(tmp_path):
    source = tmp_path / 'source'
    uploads = source / 'users/u/uploads'
    uploads.mkdir(parents=True)
    (uploads / 'sample.pdf').write_bytes(b'synthetic original')
    db = sqlite3.connect(source / 'accounts.db')
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE accounts(id INTEGER PRIMARY KEY, username TEXT)')
    db.execute("INSERT INTO accounts VALUES(1,'synthetic')")
    db.commit()
    try:
        archive = backup.create(source, tmp_path / 'backups', 7)
        restored = tmp_path / 'restored'
        backup.restore(archive, restored)
        conn = sqlite3.connect(restored / 'accounts.db')
        try:
            assert conn.execute('SELECT username FROM accounts').fetchone()[0] == 'synthetic'
        finally:
            conn.close()
        assert (restored / 'users/u/uploads/sample.pdf').read_bytes() == b'synthetic original'
        with pytest.raises(FileExistsError):
            backup.restore(archive, restored)
    finally:
        db.close()


@pytest.mark.parametrize('kind', ['traversal', 'duplicate_manifest', 'bad_hash', 'symlink'])
def test_invalid_archive_never_publishes_restore(tmp_path, kind):
    archive = tmp_path / 'invalid.tar.gz'
    name = '../escape' if kind == 'traversal' else 'accounts.db'
    manifest = {'version': 1, 'files': [{'path': name, 'size': 1, 'sha256': 'invalid'}]}
    with tarfile.open(archive, 'w:gz') as tar:
        member = tarfile.TarInfo(name); member.size = 1
        if kind == 'symlink':
            member.type = tarfile.SYMTYPE; member.linkname = '/etc/passwd'
        tar.addfile(member, io.BytesIO(b'x'))
        for _ in range(2 if kind == 'duplicate_manifest' else 1):
            content = json.dumps(manifest).encode()
            entry = tarfile.TarInfo('MANIFEST.json'); entry.size = len(content)
            tar.addfile(entry, io.BytesIO(content))
    destination = tmp_path / 'new'
    with pytest.raises(ValueError):
        backup.restore(archive, destination)
    assert not destination.exists()
    assert not (tmp_path / 'escape').exists()
