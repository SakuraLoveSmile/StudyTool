import sqlite3
import json
import logging
from pathlib import Path
from contextlib import contextmanager
from typing import Generator, Any, Optional
from app.config import settings

logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 6
STUDENT_FLOW_MIGRATION = "student_flow_v1"
LIBRARY_MIGRATION = "library_v1"


def _needs_library_migration() -> bool:
    with sqlite3.connect(settings.db_path, timeout=30.0) as conn:
        try:
            return conn.execute("SELECT value FROM schema_meta WHERE key = ?", (LIBRARY_MIGRATION,)).fetchone() is None
        except sqlite3.OperationalError:
            return True


def _needs_student_flow_migration() -> bool:
    """Feature marker keeps the additive v6 upgrade backed up exactly once."""
    with sqlite3.connect(settings.db_path, timeout=30.0) as conn:
        try:
            return conn.execute("SELECT value FROM schema_meta WHERE key = ?", (STUDENT_FLOW_MIGRATION,)).fetchone() is None
        except sqlite3.OperationalError:
            return True


def _read_schema_version() -> int:
    """Read the persisted schema version without mutating the database.

    A database without ``schema_meta`` is treated as a legacy v0 database.  This
    is important for users upgrading databases created before schema metadata was
    introduced, and also keeps fresh SQLite files on the normal initialization
    path.
    """
    if not settings.db_path.exists() or settings.db_path.stat().st_size == 0:
        return 0
    conn = sqlite3.connect(settings.db_path, timeout=30.0)
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.OperationalError as exc:
        # No schema_meta table means the database predates version tracking.
        if "no such table" in str(exc).lower():
            return 0
        raise
    finally:
        conn.close()
    if row is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"数据库 schema_version 无效: {row[0]!r}") from exc


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

@contextmanager
def db_session() -> Generator[sqlite3.Connection, None, None]:
    conn = get_db_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _table_exists(cursor: sqlite3.Cursor, table: str) -> bool:
    return cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone() is not None


def _table_columns(cursor: sqlite3.Cursor, table: str) -> set[str]:
    if not _table_exists(cursor, table):
        return set()
    return {row[1] for row in cursor.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_column_if_missing(cursor: sqlite3.Cursor, table: str, column: str, definition: str) -> None:
    """Apply additive schema changes without rewriting an existing database."""
    if not _table_exists(cursor, table):
        return
    columns = {row[1] for row in cursor.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _safe_index(cursor: sqlite3.Cursor, sql: str, name: str) -> None:
    """Create an index without making legacy upgrades fail.

    Older development snapshots may have one of the learning tables with a
    narrower shape or a conflicting UNIQUE(card_id) constraint.  The canonical
    fresh schema always gets every index; an incompatible legacy index is logged
    and retried on the next idempotent init after columns have been added.
    """
    try:
        cursor.execute(sql)
    except (sqlite3.OperationalError, sqlite3.IntegrityError) as exc:
        logger.warning("Skipping incompatible v6 index %s during legacy upgrade: %s", name, exc)


def _ensure_learning_columns(cursor: sqlite3.Cursor) -> None:
    """Add v6 columns to pre-v6/divergent learning tables in place.

    ``CREATE TABLE IF NOT EXISTS`` is intentionally not enough for an existing
    SQLite database.  This compatibility pass runs after each table has been
    created and uses only additive ALTER TABLE operations, preserving every
    existing row.  Definitions used by ALTER TABLE include safe defaults because
    SQLite cannot add a required column to a populated table without one.
    """
    columns: dict[str, tuple[tuple[str, str], ...]] = {
        "recall_chapters": (
            ("source_doc_name_snapshot", "TEXT"), ("title_normalized", "TEXT"),
            ("parent_id", "TEXT"), ("enabled", "INTEGER DEFAULT 1"),
            ("expanded_enabled", "INTEGER DEFAULT 0"), ("version", "INTEGER DEFAULT 1"),
            ("sort_order", "INTEGER DEFAULT 0"), ("metadata_json", "TEXT"),
            ("archived_at", "TIMESTAMP"),
        ),
        "recall_generation_batches": (
            ("request_id", "TEXT"), ("request_fingerprint", "TEXT"),
            ("input_snapshot_json", "TEXT"), ("source_doc_ids_json", "TEXT"),
            ("candidate_count", "INTEGER DEFAULT 0"), ("accepted_count", "INTEGER DEFAULT 0"),
            ("rejected_count", "INTEGER DEFAULT 0"), ("error_message", "TEXT"),
            ("metadata_json", "TEXT"),
        ),
        "recall_cards": (
            ("published_revision_id", "TEXT"), ("current_revision_id", "TEXT"),
            ("knowledge_point_id", "TEXT"), ("prompt", "TEXT"), ("answer", "TEXT"),
            ("hint", "TEXT"), ("source_excerpt", "TEXT"), ("tags_json", "TEXT"),
            ("publication_status", "TEXT DEFAULT 'draft'"), ("candidate", "INTEGER DEFAULT 0"),
            ("is_paused", "INTEGER DEFAULT 0"), ("is_archived", "INTEGER DEFAULT 0"),
            ("current_revision_no", "INTEGER DEFAULT 0"), ("current_revision_no_alias", "INTEGER"),
            ("lock_version", "INTEGER DEFAULT 1"),
        ),
        "recall_card_revisions": (
            ("review_status", "TEXT DEFAULT 'draft'"), ("revision_no", "INTEGER DEFAULT 0"),
            ("unit_key", "TEXT"), ("source_status", "TEXT DEFAULT 'valid'"),
            ("card_type", "TEXT DEFAULT 'basic'"), ("outline_points_json", "TEXT"),
            ("explanation", "TEXT"), ("expanded_answer", "TEXT"), ("citations_json", "TEXT"),
            ("prompt", "TEXT"), ("hint", "TEXT"), ("source_excerpt", "TEXT"), ("tags_json", "TEXT"),
            ("review_note", "TEXT"),
        ),
        "learning_settings": (
            ("daily_new_limit", "INTEGER"), ("daily_review_limit", "INTEGER"),
            ("new_cards_per_day", "INTEGER"), ("review_cards_per_day", "INTEGER"),
            ("reveal_after_seconds", "INTEGER DEFAULT 0"), ("session_minutes", "INTEGER DEFAULT 20"),
            ("timezone", "TEXT DEFAULT 'Asia/Shanghai'"),
            ("selected_chapter_ids_json", "TEXT DEFAULT '[]'"),
            ("selected_knowledge_point_ids_json", "TEXT DEFAULT '[]'"),
            ("include_unassigned", "INTEGER DEFAULT 1"), ("auto_advance", "INTEGER DEFAULT 0"),
            ("show_hints", "INTEGER DEFAULT 1"), ("version", "INTEGER DEFAULT 1"),
            ("settings_json", "TEXT"),
        ),
        "learning_progress": (
            ("chapter_id", "TEXT"), ("card_revision_id", "TEXT"), ("layer", "TEXT DEFAULT 'outline'"),
            ("content_revision_no", "INTEGER DEFAULT 0"), ("progress_type", "TEXT DEFAULT 'overall'"),
            ("state", "TEXT DEFAULT 'new'"), ("status", "TEXT DEFAULT 'new'"),
            ("interval_index", "INTEGER DEFAULT 0"), ("new_round", "INTEGER DEFAULT 0"),
            ("repetitions", "INTEGER DEFAULT 0"),
            ("ease_factor", "REAL DEFAULT 2.5"), ("interval_days", "INTEGER DEFAULT 0"),
            ("due_at", "TIMESTAMP"), ("due_date", "TEXT"), ("first_study_date", "TEXT"),
            ("last_studied_at", "TIMESTAMP"), ("last_reviewed_at", "TIMESTAMP"),
            ("review_count", "INTEGER DEFAULT 0"), ("reset_at", "TIMESTAMP"),
            ("state_version", "INTEGER DEFAULT 1"), ("version", "INTEGER DEFAULT 1"),
            ("metadata_json", "TEXT"),
        ),
        "learning_days": (
            ("study_date", "TEXT DEFAULT ''"), ("day_date", "TEXT"), ("current_round", "INTEGER DEFAULT 1"),
            ("status", "TEXT DEFAULT 'active'"), ("override_enabled", "INTEGER DEFAULT 0"),
            ("state_version", "INTEGER DEFAULT 1"), ("version", "INTEGER DEFAULT 1"),
            ("planned_count", "INTEGER DEFAULT 0"), ("completed_count", "INTEGER DEFAULT 0"),
            ("started_at", "TIMESTAMP"), ("completed_at", "TIMESTAMP"), ("metadata_json", "TEXT"),
        ),
        "learning_day_units": (
            ("learning_day_id", "TEXT"), ("study_date", "TEXT DEFAULT ''"), ("day_unit_id", "TEXT"),
            ("chapter_id", "TEXT"), ("layer", "TEXT DEFAULT 'outline'"), ("progress_id", "TEXT"),
            ("card_revision_id", "TEXT"), ("content_revision_no", "INTEGER DEFAULT 0"),
            ("unit_type", "TEXT DEFAULT 'review'"), ("display_order", "INTEGER DEFAULT 0"),
            ("position", "INTEGER DEFAULT 0"), ("started_at", "TIMESTAMP"),
            ("invalid_reason", "TEXT"), ("status", "TEXT DEFAULT 'queued'"),
        ),
        "learning_attempts": (
            ("day_unit_id", "TEXT DEFAULT ''"), ("learning_day_id", "TEXT"), ("study_date", "TEXT"),
            ("chapter_id", "TEXT"), ("layer", "TEXT DEFAULT 'outline'"), ("card_revision_id", "TEXT"),
            ("content_revision_no", "INTEGER DEFAULT 0"), ("round", "INTEGER DEFAULT 1"),
            ("attempt_kind", "TEXT DEFAULT 'primary'"), ("attempt_index", "INTEGER DEFAULT 0"),
            ("display_order", "INTEGER DEFAULT 0"), ("rating", "TEXT"), ("is_correct", "INTEGER"),
            ("answer_text", "TEXT"), ("duration_ms", "INTEGER"), ("response_json", "TEXT"),
            ("revealed_at", "TIMESTAMP"), ("submitted_at", "TIMESTAMP"), ("completed_at", "TIMESTAMP"),
            ("status", "TEXT DEFAULT 'pending'"),
        ),
        "learning_events": (
            ("request_id", "TEXT"), ("request_fingerprint", "TEXT"), ("day_unit_id", "TEXT"),
            ("study_date", "TEXT"), ("layer", "TEXT"), ("content_revision_no", "INTEGER"),
            ("rating", "TEXT"), ("before_snapshot_json", "TEXT"), ("after_snapshot_json", "TEXT"),
            ("response_json", "TEXT"),
        ),
    }
    for table, definitions in columns.items():
        if not _table_exists(cursor, table):
            continue
        for column, definition in definitions:
            _add_column_if_missing(cursor, table, column, definition)

    # Backfill aliases only when both source and destination columns exist.
    # Divergent preview schemas may contain just one side; migration must remain
    # additive and never reference a missing column.
    def _has(table: str, column: str) -> bool:
        return column in _table_columns(cursor, table)

    if _table_exists(cursor, "recall_chapters") and _has("recall_chapters", "title_normalized") and _has("recall_chapters", "normalized_title"):
        cursor.execute("UPDATE recall_chapters SET title_normalized = normalized_title WHERE title_normalized IS NULL OR title_normalized = ''")
    if _table_exists(cursor, "recall_cards"):
        if _has("recall_cards", "publication_status") and _has("recall_cards", "status"):
            cursor.execute("UPDATE recall_cards SET publication_status = CASE WHEN status = 'published' THEN 'published' ELSE COALESCE(publication_status, 'draft') END WHERE publication_status IS NULL OR publication_status = ''")
        if _has("recall_cards", "current_revision_no"):
            cursor.execute("UPDATE recall_cards SET current_revision_no = 0 WHERE current_revision_no IS NULL")
        if _has("recall_cards", "lock_version"):
            version_expr = "version" if _has("recall_cards", "version") else "1"
            cursor.execute(f"UPDATE recall_cards SET lock_version = COALESCE(lock_version, {version_expr}, 1) WHERE lock_version IS NULL OR lock_version < 1")
    if _table_exists(cursor, "recall_card_revisions"):
        if _has("recall_card_revisions", "revision_no") and _has("recall_card_revisions", "revision_number"):
            cursor.execute("UPDATE recall_card_revisions SET revision_no = revision_number WHERE revision_no IS NULL OR revision_no = 0")
        if _has("recall_card_revisions", "review_status") and _has("recall_card_revisions", "status"):
            cursor.execute("UPDATE recall_card_revisions SET review_status = status WHERE review_status IS NULL OR review_status = ''")
    if _table_exists(cursor, "learning_progress"):
        cursor.execute("UPDATE learning_progress SET layer = 'outline' WHERE layer IS NULL OR layer = ''")
        # The chapter backfill needs the v6 content tables: a v5 database with an
        # older learning_progress table has no recall_cards until the additive
        # CREATE statements below have run, so this must stay conditional.
        if _table_exists(cursor, "recall_cards") and _has("recall_cards", "chapter_id") and _has("learning_progress", "chapter_id"):
            cursor.execute("UPDATE learning_progress SET chapter_id = (SELECT chapter_id FROM recall_cards WHERE recall_cards.id = learning_progress.card_id) WHERE chapter_id IS NULL OR chapter_id = ''")
        cursor.execute("UPDATE learning_progress SET state_version = COALESCE(state_version, version, 1) WHERE state_version IS NULL OR state_version < 1")
    if _table_exists(cursor, "learning_days"):
        cursor.execute("UPDATE learning_days SET study_date = day_date WHERE (study_date IS NULL OR study_date = '') AND day_date IS NOT NULL")
        cursor.execute("UPDATE learning_days SET day_date = study_date WHERE (day_date IS NULL OR day_date = '') AND study_date IS NOT NULL")
    if _table_exists(cursor, "learning_day_units"):
        cursor.execute("UPDATE learning_day_units SET day_unit_id = id WHERE day_unit_id IS NULL OR day_unit_id = ''")
        if _table_exists(cursor, "learning_days") and _has("learning_days", "study_date"):
            cursor.execute("UPDATE learning_day_units SET study_date = (SELECT study_date FROM learning_days WHERE learning_days.id = learning_day_units.learning_day_id) WHERE (study_date IS NULL OR study_date = '')")
        cursor.execute("UPDATE learning_day_units SET study_date = '' WHERE study_date IS NULL")
    if _table_exists(cursor, "learning_attempts"):
        cursor.execute("UPDATE learning_attempts SET day_unit_id = id WHERE day_unit_id IS NULL OR day_unit_id = ''")


def _migrate_questions_year_nullable(cursor: sqlite3.Cursor) -> None:
    """Allow extracted questions to stay unassigned until a user confirms the year.

    SQLite cannot drop a NOT NULL constraint with ALTER TABLE. The migration keeps
    every existing question and relation while rebuilding only this table.
    """
    year_info = next(
        (row for row in cursor.execute("PRAGMA table_info(questions)").fetchall() if row[1] == "year"),
        None,
    )
    if not year_info or year_info[3] == 0:
        return

    connection = cursor.connection
    # End the CREATE TABLE transaction so PRAGMA foreign_keys takes effect.
    connection.commit()
    cursor.execute("PRAGMA foreign_keys = OFF")
    cursor.execute("PRAGMA legacy_alter_table = OFF")
    cursor.execute("""
        CREATE TABLE questions_new (
            id TEXT PRIMARY KEY,
            doc_id TEXT REFERENCES documents(id) ON DELETE SET NULL,
            year INTEGER,
            session_name TEXT,
            question_num TEXT,
            question_type TEXT DEFAULT 'short_answer',
            score REAL,
            stem TEXT NOT NULL,
            standard_answer TEXT,
            source_location TEXT,
            source_chunk_id TEXT,
            source_page INTEGER,
            source_paragraph_ref TEXT,
            answer_source_chunk_id TEXT,
            answer_source_doc_id TEXT,
            answer_source_page INTEGER,
            answer_source_paragraph_ref TEXT,
            needs_review INTEGER DEFAULT 1,
            review_reason TEXT,
            is_confirmed INTEGER DEFAULT 0,
            is_ai_generated INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    legacy_columns = {row[1] for row in cursor.execute("PRAGMA table_info(questions)").fetchall()}
    columns = [
        "id", "doc_id", "year", "session_name", "question_num", "question_type",
        "score", "stem", "standard_answer", "source_location", "source_chunk_id",
        "source_page", "source_paragraph_ref", "answer_source_chunk_id",
        "answer_source_doc_id", "answer_source_page", "answer_source_paragraph_ref",
        "needs_review", "review_reason", "is_confirmed", "is_ai_generated", "created_at",
    ]
    selected = [column if column in legacy_columns else "NULL" for column in columns]
    cursor.execute(
        f"INSERT INTO questions_new ({', '.join(columns)}) SELECT {', '.join(selected)} FROM questions"
    )
    cursor.execute("DROP TABLE questions")
    cursor.execute("ALTER TABLE questions_new RENAME TO questions")
    connection.commit()
    cursor.execute("PRAGMA foreign_keys = ON")
    foreign_key_errors = cursor.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise RuntimeError(f"questions 表迁移后存在外键错误: {foreign_key_errors[:3]}")

def _create_learning_schema(cursor: sqlite3.Cursor) -> None:
    """Create the additive learning/content tables introduced in schema v6.

    The tables deliberately do not alter or reinterpret legacy content tables.  A
    card's source is copied into revision snapshots, while foreign keys back to
    the existing documents/chunks tables are used only for validation and
    navigation.  This lets old imports survive future edits/deletions and keeps
    generated candidates out of the published learning set.
    """
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS recall_chapters (
            id TEXT PRIMARY KEY,
            source_doc_id TEXT REFERENCES documents(id) ON DELETE SET NULL,
            source_doc_name_snapshot TEXT,
            title TEXT NOT NULL,
            normalized_title TEXT NOT NULL,
            title_normalized TEXT,
            parent_id TEXT REFERENCES recall_chapters(id) ON DELETE SET NULL,
            status TEXT NOT NULL DEFAULT 'active',
            enabled INTEGER NOT NULL DEFAULT 1,
            expanded_enabled INTEGER NOT NULL DEFAULT 0,
            version INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            archived_at TIMESTAMP
        )
    """)
    _safe_index(cursor, """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_recall_chapters_source_title
        ON recall_chapters(source_doc_id, normalized_title)
    """, "uq_recall_chapters_source_title")
    _safe_index(cursor, """
        CREATE INDEX IF NOT EXISTS idx_recall_chapters_source_doc
        ON recall_chapters(source_doc_id, status, sort_order)
    """, "idx_recall_chapters_source_doc")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS recall_generation_batches (
            id TEXT PRIMARY KEY,
            chapter_id TEXT REFERENCES recall_chapters(id) ON DELETE SET NULL,
            source_doc_id TEXT REFERENCES documents(id) ON DELETE SET NULL,
            request_id TEXT,
            request_fingerprint TEXT,
            input_snapshot_json TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            source_doc_ids_json TEXT,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            accepted_count INTEGER NOT NULL DEFAULT 0,
            rejected_count INTEGER NOT NULL DEFAULT 0,
            error_message TEXT,
            metadata_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    _safe_index(cursor, """
        CREATE INDEX IF NOT EXISTS idx_recall_generation_batches_chapter
        ON recall_generation_batches(chapter_id, created_at)
    """, "idx_recall_generation_batches_chapter")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS recall_cards (
            id TEXT PRIMARY KEY,
            chapter_id TEXT NOT NULL REFERENCES recall_chapters(id) ON DELETE CASCADE,
            source_doc_id TEXT REFERENCES documents(id) ON DELETE SET NULL,
            generation_batch_id TEXT REFERENCES recall_generation_batches(id) ON DELETE SET NULL,
            knowledge_point_id TEXT REFERENCES knowledge_points(id) ON DELETE SET NULL,
            title TEXT,
            prompt TEXT,
            answer TEXT,
            hint TEXT,
            source_excerpt TEXT,
            tags_json TEXT,
            card_type TEXT NOT NULL DEFAULT 'basic',
            origin TEXT NOT NULL DEFAULT 'manual',
            status TEXT NOT NULL DEFAULT 'draft',
            publication_status TEXT NOT NULL DEFAULT 'draft',
            is_confirmed INTEGER NOT NULL DEFAULT 0,
            is_ai_generated INTEGER NOT NULL DEFAULT 0,
            candidate INTEGER NOT NULL DEFAULT 0,
            is_paused INTEGER NOT NULL DEFAULT 0,
            is_archived INTEGER NOT NULL DEFAULT 0,
            sort_order INTEGER NOT NULL DEFAULT 0,
            current_revision_id TEXT,
            current_revision_no INTEGER NOT NULL DEFAULT 0,
            current_revision_no_alias INTEGER,
            published_revision_id TEXT,
            version INTEGER NOT NULL DEFAULT 1,
            lock_version INTEGER NOT NULL DEFAULT 1,
            metadata_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            published_at TIMESTAMP,
            archived_at TIMESTAMP
        )
    """)
    _safe_index(cursor, """
        CREATE INDEX IF NOT EXISTS idx_recall_cards_chapter_status
        ON recall_cards(chapter_id, status, sort_order)
    """, "idx_recall_cards_chapter_status")
    _safe_index(cursor, """
        CREATE INDEX IF NOT EXISTS idx_recall_cards_learning_visible
        ON recall_cards(chapter_id, is_confirmed, is_ai_generated, status)
    """, "idx_recall_cards_learning_visible")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS recall_card_revisions (
            id TEXT PRIMARY KEY,
            card_id TEXT NOT NULL REFERENCES recall_cards(id) ON DELETE CASCADE,
            revision_number INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            review_status TEXT NOT NULL DEFAULT 'draft',
            revision_no INTEGER NOT NULL DEFAULT 0,
            unit_key TEXT,
            source_status TEXT NOT NULL DEFAULT 'valid',
            card_type TEXT NOT NULL DEFAULT 'basic',
            outline_points_json TEXT,
            explanation TEXT,
            expanded_answer TEXT,
            citations_json TEXT,
            content_json TEXT,
            front_content TEXT,
            back_content TEXT,
            prompt TEXT,
            hint TEXT,
            source_excerpt TEXT,
            tags_json TEXT,
            question TEXT,
            answer TEXT,
            concise_content TEXT,
            detailed_content TEXT,
            answer_structure TEXT,
            confusing_points_and_cases TEXT,
            source_citations_json TEXT NOT NULL DEFAULT '[]',
            base_revision_id TEXT REFERENCES recall_card_revisions(id) ON DELETE SET NULL,
            created_by TEXT,
            reviewed_by TEXT,
            review_note TEXT,
            rejection_reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            published_at TIMESTAMP,
            UNIQUE(card_id, revision_number)
        )
    """)
    _safe_index(cursor, """
        CREATE INDEX IF NOT EXISTS idx_recall_card_revisions_card
        ON recall_card_revisions(card_id, revision_number DESC, status)
    """, "idx_recall_card_revisions_card")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS learning_settings (
            id TEXT PRIMARY KEY,
            daily_new_limit INTEGER,
            daily_review_limit INTEGER,
            new_cards_per_day INTEGER,
            review_cards_per_day INTEGER,
            reveal_after_seconds INTEGER NOT NULL DEFAULT 0,
            session_minutes INTEGER NOT NULL DEFAULT 20,
            timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
            selected_chapter_ids_json TEXT NOT NULL DEFAULT '[]',
            selected_knowledge_point_ids_json TEXT NOT NULL DEFAULT '[]',
            include_unassigned INTEGER NOT NULL DEFAULT 1,
            auto_advance INTEGER NOT NULL DEFAULT 0,
            show_hints INTEGER NOT NULL DEFAULT 1,
            version INTEGER NOT NULL DEFAULT 1,
            settings_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS learning_progress (
            id TEXT PRIMARY KEY,
            card_id TEXT NOT NULL REFERENCES recall_cards(id) ON DELETE CASCADE,
            chapter_id TEXT NOT NULL REFERENCES recall_chapters(id) ON DELETE CASCADE,
            card_revision_id TEXT REFERENCES recall_card_revisions(id) ON DELETE SET NULL,
            layer TEXT NOT NULL DEFAULT 'outline',
            content_revision_no INTEGER NOT NULL DEFAULT 0,
            progress_type TEXT NOT NULL DEFAULT 'overall',
            state TEXT NOT NULL DEFAULT 'new',
            status TEXT NOT NULL DEFAULT 'new',
            interval_index INTEGER NOT NULL DEFAULT 0,
            new_round INTEGER NOT NULL DEFAULT 0,
            repetitions INTEGER NOT NULL DEFAULT 0,
            ease_factor REAL NOT NULL DEFAULT 2.5,
            interval_days INTEGER NOT NULL DEFAULT 0,
            due_at TIMESTAMP,
            due_date TEXT,
            first_study_date TEXT,
            last_studied_at TIMESTAMP,
            last_reviewed_at TIMESTAMP,
            review_count INTEGER NOT NULL DEFAULT 0,
            reset_at TIMESTAMP,
            state_version INTEGER NOT NULL DEFAULT 1,
            version INTEGER NOT NULL DEFAULT 1,
            metadata_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(card_id, layer)
        )
    """)
    _safe_index(cursor, """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_learning_progress_card_layer
        ON learning_progress(card_id, layer)
    """, "uq_learning_progress_card_layer")
    _safe_index(cursor, """
        CREATE INDEX IF NOT EXISTS idx_learning_progress_due
        ON learning_progress(chapter_id, status, due_date, due_at)
    """, "idx_learning_progress_due")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS learning_days (
            id TEXT PRIMARY KEY,
            study_date TEXT NOT NULL UNIQUE,
            day_date TEXT,
            current_round INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'active',
            override_enabled INTEGER NOT NULL DEFAULT 0,
            state_version INTEGER NOT NULL DEFAULT 1,
            version INTEGER NOT NULL DEFAULT 1,
            planned_count INTEGER NOT NULL DEFAULT 0,
            completed_count INTEGER NOT NULL DEFAULT 0,
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            metadata_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_learning_days_status_date
        ON learning_days(status, day_date)
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS learning_day_units (
            id TEXT PRIMARY KEY,
            learning_day_id TEXT REFERENCES learning_days(id) ON DELETE CASCADE,
            study_date TEXT NOT NULL,
            day_unit_id TEXT,
            card_id TEXT NOT NULL REFERENCES recall_cards(id) ON DELETE CASCADE,
            chapter_id TEXT NOT NULL REFERENCES recall_chapters(id) ON DELETE CASCADE,
            layer TEXT NOT NULL DEFAULT 'outline',
            progress_id TEXT REFERENCES learning_progress(id) ON DELETE SET NULL,
            card_revision_id TEXT REFERENCES recall_card_revisions(id) ON DELETE SET NULL,
            content_revision_no INTEGER NOT NULL DEFAULT 0,
            unit_type TEXT NOT NULL DEFAULT 'review',
            display_order INTEGER NOT NULL DEFAULT 0,
            position INTEGER NOT NULL DEFAULT 0,
            started_at TIMESTAMP,
            invalid_reason TEXT,
            status TEXT NOT NULL DEFAULT 'queued',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(study_date, card_id, layer)
        )
    """)
    _safe_index(cursor, """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_learning_day_units_date_card_layer
        ON learning_day_units(study_date, card_id, layer)
    """, "uq_learning_day_units_date_card_layer")
    _safe_index(cursor, """
        CREATE INDEX IF NOT EXISTS idx_learning_day_units_day_status
        ON learning_day_units(study_date, status, display_order, position)
    """, "idx_learning_day_units_day_status")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS learning_attempts (
            id TEXT PRIMARY KEY,
            day_unit_id TEXT NOT NULL,
            learning_day_id TEXT REFERENCES learning_days(id) ON DELETE SET NULL,
            study_date TEXT,
            card_id TEXT NOT NULL REFERENCES recall_cards(id) ON DELETE CASCADE,
            chapter_id TEXT NOT NULL REFERENCES recall_chapters(id) ON DELETE CASCADE,
            layer TEXT NOT NULL DEFAULT 'outline',
            card_revision_id TEXT REFERENCES recall_card_revisions(id) ON DELETE SET NULL,
            content_revision_no INTEGER NOT NULL DEFAULT 0,
            round INTEGER NOT NULL DEFAULT 1,
            attempt_kind TEXT NOT NULL DEFAULT 'primary',
            attempt_index INTEGER NOT NULL DEFAULT 0,
            display_order INTEGER NOT NULL DEFAULT 0,
            rating TEXT,
            is_correct INTEGER,
            answer_text TEXT,
            duration_ms INTEGER,
            response_json TEXT,
            revealed_at TIMESTAMP,
            submitted_at TIMESTAMP,
            completed_at TIMESTAMP,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(day_unit_id, round, attempt_index)
        )
    """)
    _safe_index(cursor, """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_learning_attempts_unit_round_index
        ON learning_attempts(day_unit_id, round, attempt_index)
    """, "uq_learning_attempts_unit_round_index")
    _safe_index(cursor, """
        CREATE INDEX IF NOT EXISTS idx_learning_attempts_card_time
        ON learning_attempts(card_id, created_at DESC)
    """, "idx_learning_attempts_card_time")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS learning_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT UNIQUE,
            request_fingerprint TEXT,
            event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT,
            day_unit_id TEXT,
            study_date TEXT,
            card_id TEXT REFERENCES recall_cards(id) ON DELETE SET NULL,
            chapter_id TEXT REFERENCES recall_chapters(id) ON DELETE SET NULL,
            learning_day_id TEXT REFERENCES learning_days(id) ON DELETE SET NULL,
            layer TEXT,
            content_revision_no INTEGER,
            rating TEXT,
            before_snapshot_json TEXT,
            after_snapshot_json TEXT,
            response_json TEXT,
            data_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    _safe_index(cursor, """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_learning_events_request_id
        ON learning_events(request_id)
    """, "uq_learning_events_request_id")
    _safe_index(cursor, """
        CREATE INDEX IF NOT EXISTS idx_learning_events_entity_time
        ON learning_events(entity_type, entity_id, id)
    """, "idx_learning_events_entity_time")
    _safe_index(cursor, """
        CREATE INDEX IF NOT EXISTS idx_learning_events_chapter_time
        ON learning_events(chapter_id, id)
    """, "idx_learning_events_chapter_time")


def backup_database(destination_path: Optional[Path] = None) -> Optional[Path]:
    """Create a point-in-time snapshot backup using SQLite Online Backup API."""
    if not settings.db_path.exists() or settings.db_path.stat().st_size == 0:
        return None
    from datetime import datetime
    dest = destination_path or (settings.data_path / f"rag_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")
    src_conn = sqlite3.connect(settings.db_path)
    try:
        dst_conn = sqlite3.connect(dest)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()
    logger.info("Database snapshot created at %s", dest)
    return dest


def init_db() -> None:
    """Initialize the legacy schema and apply the additive v6 learning schema.

    Initialization is deliberately fail-closed for upgrades: an existing
    database at a version below ``CURRENT_SCHEMA_VERSION`` must be backed up
    before *any* schema mutation.  A failed backup is propagated to the caller,
    so startup cannot continue against an only-partially-protected database.
    """
    settings.data_path.mkdir(parents=True, exist_ok=True)
    existing_database = settings.db_path.exists() and settings.db_path.stat().st_size > 0
    previous_version = _read_schema_version() if existing_database else 0
    if previous_version > CURRENT_SCHEMA_VERSION:
        raise RuntimeError(
            f"数据库 schema_version={previous_version} 高于当前支持版本 {CURRENT_SCHEMA_VERSION}，拒绝降级"
        )
    if existing_database and (previous_version < CURRENT_SCHEMA_VERSION or _needs_student_flow_migration() or _needs_library_migration()):
        # Do not catch this exception: backup failure must abort the upgrade.
        backup_database()

    with db_session() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS system_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 1. Documents
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            file_hash TEXT UNIQUE NOT NULL,
            file_type TEXT NOT NULL, -- 'textbook', 'exam', 'answer', 'supplement'
            year INTEGER,
            session_name TEXT,       -- e.g. 'A卷', 'B卷'
            status TEXT DEFAULT 'pending', -- 'pending', 'processing', 'completed', 'failed'
            error_msg TEXT,
            page_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # 2. Document Chunks
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS document_chunks (
            id TEXT PRIMARY KEY,
            doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL,
            page_number INTEGER,     -- Physical PDF page number (1-based) or None for DOCX
            heading TEXT,            -- Chapter or section heading
            paragraph_ref TEXT,      -- DOCX paragraph reference (e.g. '段落 12-15')
            content TEXT NOT NULL,
            content_segmented TEXT,  -- jieba-segmented text for FTS
            embedding BLOB,          -- float32 array bytes
            original_content TEXT,
            embedding_status TEXT DEFAULT 'pending',
            parse_error TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # 3. FTS5 Virtual Table for full-text search
        cursor.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS document_chunks_fts USING fts5(
            chunk_id UNINDEXED,
            content_segmented,
            tokenize = 'unicode61'
        );
        """)

        # 4. Knowledge Points
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_points (
            id TEXT PRIMARY KEY,
            code TEXT,               -- e.g. '1.1', '2.3.1'
            name TEXT NOT NULL,
            chapter TEXT,
            parent_id TEXT REFERENCES knowledge_points(id) ON DELETE SET NULL,
            priority_tag TEXT DEFAULT 'normal', -- 'important', 'normal', 'deferred'
            sort_order INTEGER DEFAULT 0,
            source_type TEXT DEFAULT 'pending_verification', -- 'pending_verification', 'extracted', 'manual'
            source_status TEXT DEFAULT 'valid',              -- 'valid', 'invalid', 'supplement_needed'
            source_reference TEXT,
            is_deleted INTEGER DEFAULT 0,
            deleted_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # 5. Questions
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS questions (
            id TEXT PRIMARY KEY,
            doc_id TEXT REFERENCES documents(id) ON DELETE SET NULL,
            year INTEGER,
            session_name TEXT,       -- e.g. 'A卷'
            question_num TEXT,       -- e.g. '一、1' or '简答题 3'
            question_type TEXT DEFAULT 'short_answer', -- 'short_answer', 'essay', 'case_study'
            score REAL,
            stem TEXT NOT NULL,
            standard_answer TEXT,
            source_location TEXT,    -- e.g. '2021年真题 P3' or 'DOCX 段落 42'
            source_chunk_id TEXT,
            source_page INTEGER,
            source_paragraph_ref TEXT,
            answer_source_chunk_id TEXT,
            answer_source_doc_id TEXT,
            answer_source_page INTEGER,
            answer_source_paragraph_ref TEXT,
            needs_review INTEGER DEFAULT 1,
            review_reason TEXT,
            is_confirmed INTEGER DEFAULT 0, -- 0: pending human check, 1: confirmed
            is_ai_generated INTEGER DEFAULT 0, -- 1: simulated question
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # 6. Question Knowledge Point Mapping (Many-to-Many)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS question_knowledge_rel (
            id TEXT PRIMARY KEY,
            question_id TEXT NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
            knowledge_point_id TEXT NOT NULL REFERENCES knowledge_points(id) ON DELETE CASCADE,
            UNIQUE(question_id, knowledge_point_id)
        );
        """)

        # Answers uploaded separately remain candidates until a user explicitly
        # attaches one to a question from a matching year/number.
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS answer_candidates (
            id TEXT PRIMARY KEY,
            doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            year INTEGER,
            session_name TEXT,
            question_num TEXT,
            answer_text TEXT NOT NULL,
            source_chunk_id TEXT,
            source_page INTEGER,
            source_paragraph_ref TEXT,
            status TEXT DEFAULT 'pending',
            question_id TEXT REFERENCES questions(id) ON DELETE SET NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # 7. Study Cards
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS study_cards (
            id TEXT PRIMARY KEY,
            knowledge_point_id TEXT UNIQUE NOT NULL REFERENCES knowledge_points(id) ON DELETE CASCADE,
            concise_content TEXT,    -- 精简背诵版 (定义、核心要点、关键词)
            detailed_content TEXT,   -- 展开答题版 (完整论述)
            answer_structure TEXT,   -- 答题结构 (概念 -> 特征 -> 条件 -> 案例)
            confusing_points_and_cases TEXT, -- 易混点与教材实际案例 (无案例明确标注缺失)
            citations_json TEXT,     -- 引用详情 JSON: [{chunk_id, doc_name, page_or_ref, snippet}]
            needs_update INTEGER DEFAULT 0, -- 1 if related questions/text edited
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # 8. Practice Sessions
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS practice_sessions (
            id TEXT PRIMARY KEY,
            knowledge_point_id TEXT NOT NULL REFERENCES knowledge_points(id) ON DELETE CASCADE,
            question_count INTEGER NOT NULL,
            difficulty TEXT NOT NULL, -- 'basic', 'comprehensive'
            questions_json TEXT NOT NULL, -- [{question_id, stem, reference_answer, evaluation_points, basis}]
            user_answers_json TEXT,   -- [{question_id, answer_text}]
            evaluation_json TEXT,     -- 批改结果 JSON (各项覆盖判断, 错误指出, 改写建议, 估分)
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # 9. Persistent Tasks
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            task_type TEXT NOT NULL,  -- 'parse_document', 'extract_questions', 'generate_card', 'batch_link', 'extract_knowledge_points'
            target_id TEXT,           -- document_id or knowledge_point_id
            status TEXT DEFAULT 'pending', -- 'pending', 'processing', 'cancel_requested', 'cancelled', 'completed', 'partial', 'failed'
            progress INTEGER DEFAULT 0,   -- 0 to 100
            message TEXT,
            error_msg TEXT,
            batch_errors_json TEXT,
            retry_of TEXT,
            hidden_at TIMESTAMP,
            clear_token TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # 10. Extraction Batches & Candidates
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS extraction_batches (
            id TEXT PRIMARY KEY,
            task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
            doc_ids_json TEXT NOT NULL,
            status TEXT DEFAULT 'processing', -- 'processing', 'completed', 'cancelled', 'failed', 'partial'
            coverage_info_json TEXT,
            error_msg TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS extraction_candidates (
            id TEXT PRIMARY KEY,
            extraction_id TEXT NOT NULL REFERENCES extraction_batches(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            chapter TEXT,
            definition TEXT,
            citations_json TEXT NOT NULL,
            status TEXT DEFAULT 'pending', -- 'pending', 'accepted', 'ignored', 'merged'
            match_type TEXT DEFAULT 'new', -- 'new', 'existing', 'source_changed', 'deleted_or_ignored'
            matched_kp_id TEXT REFERENCES knowledge_points(id) ON DELETE SET NULL,
            target_kp_id TEXT REFERENCES knowledge_points(id) ON DELETE SET NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # 11. Task Events (Timeline / Stage transitions / Provider waits / Retries)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            event_type TEXT NOT NULL,
            stage TEXT,
            unit_index INTEGER,
            message TEXT NOT NULL,
            data_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_task_events_task_id_id ON task_events(task_id, id);
        """)

        # 12. Task Checkpoints (Incremental resume for batches/pages)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS task_checkpoints (
            id TEXT PRIMARY KEY,
            task_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            unit_key TEXT NOT NULL,
            unit_index INTEGER NOT NULL,
            status TEXT NOT NULL,
            result_count INTEGER DEFAULT 0,
            data_json TEXT,
            error_msg TEXT,
            model_version TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(task_type, target_id, unit_key)
        );
        """)
        cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_task_checkpoints_lookup ON task_checkpoints(task_type, target_id);
        """)

        # Migrations
        for table, columns in {
            "extraction_batches": (("source_status", "TEXT DEFAULT 'valid'"), ("source_snapshot_hash", "TEXT")),
            "extraction_candidates": (("source_status", "TEXT DEFAULT 'valid'"), ("decision_json", "TEXT"), ("version", "INTEGER NOT NULL DEFAULT 1")),
        }.items():
            for column, definition in columns:
                _add_column_if_missing(cursor, table, column, definition)
        _migrate_questions_year_nullable(cursor)
        for column, definition in (
            ("source_chunk_id", "TEXT"),
            ("source_page", "INTEGER"),
            ("source_paragraph_ref", "TEXT"),
            ("answer_source_chunk_id", "TEXT"),
            ("answer_source_doc_id", "TEXT"),
            ("answer_source_page", "INTEGER"),
            ("answer_source_paragraph_ref", "TEXT"),
            ("needs_review", "INTEGER DEFAULT 1"),
            ("review_reason", "TEXT"),
        ):
            _add_column_if_missing(cursor, "questions", column, definition)

        for column, definition in (
            ("original_content", "TEXT"),
            ("embedding_status", "TEXT DEFAULT 'pending'"),
            ("parse_error", "TEXT"),
            ("updated_at", "TIMESTAMP"),
        ):
            _add_column_if_missing(cursor, "document_chunks", column, definition)

        _add_column_if_missing(cursor, "tasks", "batch_errors_json", "TEXT")
        _add_column_if_missing(cursor, "tasks", "retry_of", "TEXT")
        _add_column_if_missing(cursor, "tasks", "hidden_at", "TIMESTAMP")
        _add_column_if_missing(cursor, "tasks", "clear_token", "TEXT")
        _add_column_if_missing(cursor, "tasks", "progress_detail_json", "TEXT")
        _add_column_if_missing(cursor, "tasks", "timing_breakdown_json", "TEXT")
        _add_column_if_missing(cursor, "tasks", "parent_task_id", "TEXT")
        _add_column_if_missing(cursor, "tasks", "started_at", "TIMESTAMP")

        _add_column_if_missing(cursor, "knowledge_points", "source_type", "TEXT DEFAULT 'pending_verification'")
        _add_column_if_missing(cursor, "knowledge_points", "source_status", "TEXT DEFAULT 'valid'")
        _add_column_if_missing(cursor, "knowledge_points", "source_reference", "TEXT")
        _add_column_if_missing(cursor, "knowledge_points", "is_deleted", "INTEGER DEFAULT 0")
        _add_column_if_missing(cursor, "knowledge_points", "deleted_at", "TIMESTAMP")

        # Migrate existing rows
        cursor.execute("UPDATE knowledge_points SET source_type = 'pending_verification' WHERE source_type IS NULL")
        cursor.execute("UPDATE knowledge_points SET source_status = 'valid' WHERE source_status IS NULL")
        cursor.execute("UPDATE knowledge_points SET is_deleted = 0 WHERE is_deleted IS NULL")

        # v6 is intentionally additive; legacy rows/tables are never rewritten.
        # A fresh database has already been initialized above, while an existing
        # v5 (or older) database receives the same idempotent CREATE IF NOT EXISTS
        # statements.  Older development snapshots may already contain narrower
        # learning tables, so add the canonical compatibility columns before the
        # v6 indexes are created.
        _ensure_learning_columns(cursor)
        _create_learning_schema(cursor)

        # A timestamp on an approved historical revision is publication evidence.
        # Never infer publication from the current (possibly unreviewed) draft.
        cursor.execute("""
            UPDATE recall_cards SET published_revision_id = (
                SELECT r.id FROM recall_card_revisions r
                WHERE r.card_id = recall_cards.id AND r.published_at IS NOT NULL
                  AND (r.review_status = 'approved' OR r.status = 'approved')
                ORDER BY r.published_at DESC, r.revision_number DESC LIMIT 1
            )
            WHERE published_revision_id IS NULL AND is_confirmed = 1
              AND publication_status IN ('published', 'approved')
        """)
        cursor.execute("INSERT OR IGNORE INTO schema_meta (key, value) VALUES (?, '1')", (STUDENT_FLOW_MIGRATION,))

        # No source foreign keys: saved learning history survives source deletion
        # and is explicitly marked stale when its persisted snapshot diverges.
        for table in ("library_guides", "library_answers"):
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS {table} (
                    id TEXT PRIMARY KEY,
                    doc_id TEXT,
                    chapter_id TEXT,
                    payload_json TEXT NOT NULL,
                    source_snapshot_json TEXT NOT NULL,
                    source_stale INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_scope ON {table}(doc_id, chapter_id)")
        cursor.execute("INSERT OR IGNORE INTO schema_meta (key, value) VALUES (?, '1')", (LIBRARY_MIGRATION,))

        cursor.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(CURRENT_SCHEMA_VERSION),),
        )
        logger.info("Database initialized successfully at %s (schema v%s)", settings.db_path, CURRENT_SCHEMA_VERSION)


def get_system_metadata(key: str, default: Optional[str] = None) -> Optional[str]:
    """Retrieve a value from the persisted system_metadata table."""
    try:
        with db_session() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='system_metadata'")
            if not cursor.fetchone():
                return default
            cursor.execute("SELECT value FROM system_metadata WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row[0] if row else default
    except Exception:
        return default


def set_system_metadata(key: str, value: str) -> None:
    """Store or update a value in the persisted system_metadata table."""
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS system_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute(
            """INSERT INTO system_metadata (key, value, updated_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP""",
            (key, str(value)),
        )


def is_vector_locked() -> tuple[bool, Optional[str]]:
    """Check if vector operations (retrieval & append) are locked.
    Returns (is_locked: bool, reason: Optional[str]).
    """
    locked_val = get_system_metadata("vector_locked")
    if locked_val and locked_val.lower() in {"true", "1", "yes"}:
        reason = get_system_metadata("vector_lock_reason", "向量索引已锁定，请在设置中重建索引")
        return True, reason

    # Check if vectors exist in document_chunks
    try:
        with db_session() as conn:
            cursor = conn.cursor()
            has_chunks = cursor.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_chunks'"
            ).fetchone()
            if not has_chunks:
                return False, None
            row = cursor.execute(
                "SELECT 1 FROM document_chunks WHERE embedding IS NOT NULL LIMIT 1"
            ).fetchone()
            if row:
                meta_model = get_system_metadata("embedding_model")
                meta_url = get_system_metadata("embedding_base_url")
                if not (meta_model or "").strip():
                    reason = "已有向量但来源信息缺失，已锁定向量检索与追加，要求在设置中重建索引后才能恢复使用"
                    set_system_metadata("vector_locked", "true")
                    set_system_metadata("vector_lock_reason", reason)
                    return True, reason
                # If current settings differ from persisted metadata, enforce lock
                if settings.EMBEDDING_MODEL and meta_model and settings.EMBEDDING_MODEL != meta_model:
                    reason = f"当前配置向量模型 ({settings.EMBEDDING_MODEL}) 与历史向量模型 ({meta_model}) 不一致，已锁定检索与追加"
                    return True, reason
                if (
                    settings.EMBEDDING_BASE_URL
                    and meta_url
                    and settings.EMBEDDING_BASE_URL.rstrip("/").lower() != meta_url.rstrip("/").lower()
                ):
                    reason = "当前配置向量服务地址与历史向量服务地址不一致，已锁定检索与追加"
                    return True, reason
    except Exception as exc:
        logger.warning("Error checking vector lock status: %s", exc)

    return False, None
