"""Migration drill: a disposable v5 database must upgrade to v6 safely.

The drill builds a *legacy* database by hand (v5 marker, NOT NULL ``questions.year``,
a narrow pre-v6 ``learning_progress`` table and legacy rows), then runs the real
``init_db()`` upgrade path and checks:

* an online-backup snapshot is written before any schema change;
* legacy rows survive the upgrade verbatim;
* ``questions.year`` becomes nullable while every question row is preserved;
* the narrow learning table gains the canonical v6 columns without data loss;
* the version marker ends at v6 and the upgrade is idempotent.
"""
from __future__ import annotations

import sqlite3

from app.config import settings
from app.database import get_db_connection, init_db
from app.services.learning_service import LearningService
from app.services.recall_cards import RecallCardsService

LEGACY_VERSION = 5


def _build_v5_database(path: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO schema_meta (key, value) VALUES ('schema_version', '5');

            CREATE TABLE documents (
                id TEXT PRIMARY KEY, filename TEXT NOT NULL, file_hash TEXT UNIQUE NOT NULL,
                file_type TEXT NOT NULL, year INTEGER, session_name TEXT,
                status TEXT DEFAULT 'pending', error_msg TEXT, page_count INTEGER DEFAULT 0
            );
            INSERT INTO documents (id, filename, file_hash, file_type, year, status)
            VALUES ('doc-legacy', '教材第五章.pdf', 'hash-legacy', 'textbook', 2024, 'completed');

            CREATE TABLE document_chunks (
                id TEXT PRIMARY KEY, doc_id TEXT REFERENCES documents(id) ON DELETE CASCADE,
                content TEXT NOT NULL, chunk_index INTEGER DEFAULT 0, token_count INTEGER DEFAULT 0
            );
            INSERT INTO document_chunks (id, doc_id, content, chunk_index)
            VALUES ('chunk-1', 'doc-legacy', '回忆卡的原文依据', 0);

            -- v5 stored year as NOT NULL.
            CREATE TABLE questions (
                id TEXT PRIMARY KEY,
                doc_id TEXT REFERENCES documents(id) ON DELETE SET NULL,
                year INTEGER NOT NULL,
                session_name TEXT,
                question_num TEXT,
                question_type TEXT DEFAULT 'short_answer',
                score REAL,
                stem TEXT NOT NULL,
                standard_answer TEXT,
                source_location TEXT,
                is_confirmed INTEGER DEFAULT 0,
                is_ai_generated INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO questions (id, doc_id, year, question_num, stem, standard_answer)
            VALUES ('q-legacy', 'doc-legacy', 2023, '12', '简述间隔重复原理', '按遗忘曲线安排复习');

            CREATE TABLE knowledge_points (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO knowledge_points (id, name) VALUES ('kp-legacy', '间隔重复');

            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, task_type TEXT NOT NULL, status TEXT DEFAULT 'pending'
            );

            -- A narrower pre-v6 learning_progress table: missing most v6 columns.
            CREATE TABLE learning_progress (
                id TEXT PRIMARY KEY,
                card_id TEXT NOT NULL,
                layer TEXT NOT NULL DEFAULT 'outline',
                state TEXT NOT NULL DEFAULT 'new',
                interval_index INTEGER NOT NULL DEFAULT 0,
                due_date TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO learning_progress (id, card_id, layer, state, interval_index)
            VALUES ('legacy-progress', 'legacy-card', 'outline', 'learning', 2);
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_v5_database_upgrades_to_v6_with_backup_and_preserved_data(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    _build_v5_database(settings.db_path)

    init_db()

    backups = sorted(p.name for p in tmp_path.glob("rag_backup_*.db"))
    assert backups, "an existing older database must be snapshotted before upgrading"
    snapshot = sqlite3.connect(str(tmp_path / backups[0]))
    try:
        assert snapshot.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0] == str(LEGACY_VERSION)
        assert snapshot.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 1
    finally:
        snapshot.close()

    conn = get_db_connection()
    try:
        assert conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()[0] == "6"

        # Legacy content rows survive verbatim.
        doc = conn.execute("SELECT * FROM documents WHERE id = 'doc-legacy'").fetchone()
        assert doc["filename"] == "教材第五章.pdf"
        question = conn.execute("SELECT * FROM questions WHERE id = 'q-legacy'").fetchone()
        assert question["stem"] == "简述间隔重复原理"
        assert question["year"] == 2023
        assert conn.execute("SELECT COUNT(*) FROM knowledge_points WHERE id = 'kp-legacy'").fetchone()[0] == 1

        # year is now nullable, so an unconfirmed extraction can exist.
        year_column = next(row for row in conn.execute("PRAGMA table_info(questions)") if row[1] == "year")
        assert year_column[3] == 0, "questions.year must become nullable"
        conn.execute(
            "INSERT INTO questions (id, doc_id, year, stem) VALUES ('q-new', 'doc-legacy', NULL, '未定年份题目')"
        )
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 2

        # The narrow learning table gained canonical v6 columns and kept its row.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(learning_progress)")}
        for expected in ("chapter_id", "card_revision_id", "status", "review_count", "state_version", "content_revision_no"):
            assert expected in columns, expected
        legacy_row = conn.execute("SELECT * FROM learning_progress WHERE id = 'legacy-progress'").fetchone()
        assert legacy_row["card_id"] == "legacy-card"
        assert legacy_row["interval_index"] == 2

        # The upgraded database is fully usable: publish a card and study it.
        assert conn.execute("SELECT COUNT(*) FROM recall_cards").fetchone()[0] == 0
        LearningService.update_settings(
            {"daily_new_limit": 2, "daily_review_limit": 2, "timezone": "Asia/Shanghai"}, conn=conn
        )
        chapter = RecallCardsService.create_chapter({"title": "迁移后章节"}, conn=conn)
        card = RecallCardsService.create_card(
            {"chapter_id": chapter["id"], "prompt": "迁移后问题", "answer": "迁移后答案"}, conn=conn
        )
        RecallCardsService.review_revisions(
            {"revisions": [{"revision_id": card["revision_id"], "decision": "approve"}]}, conn=conn
        )
        RecallCardsService.publish_card(card["id"], revision_id=card["revision_id"], conn=conn)
        opened = LearningService.open_today(conn=conn, study_date="2026-05-06")
        assert opened["counts"]["total"] == 1
        attempt_id = opened["next_item"]["attempt_id"]
        LearningService.reveal(conn=conn, study_date="2026-05-06", attempt_id=attempt_id)
        rated = LearningService.rate(
            conn=conn, study_date="2026-05-06", attempt_id=attempt_id, rating="fuzzy", request_id="mig-1"
        )
        assert rated["rating"] == "fuzzy"
    finally:
        conn.close()

    # Re-running initialization on an already upgraded database is a no-op.
    init_db()
    conn = get_db_connection()
    try:
        assert conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()[0] == "6"
        assert conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM recall_cards").fetchone()[0] == 1
    finally:
        conn.close()

    # Exactly one snapshot was taken: the second init had nothing to upgrade.
    assert len(sorted(tmp_path.glob("rag_backup_*.db"))) == 1


def test_downgrade_is_refused_for_newer_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    init_db()
    conn = get_db_connection()
    try:
        conn.execute("UPDATE schema_meta SET value = '99' WHERE key = 'schema_version'")
        conn.commit()
    finally:
        conn.close()

    try:
        init_db()
    except RuntimeError as error:
        assert "拒绝降级" in str(error)
    else:  # pragma: no cover - the guard is fail-closed
        raise AssertionError("init_db must refuse to open a newer schema version")
