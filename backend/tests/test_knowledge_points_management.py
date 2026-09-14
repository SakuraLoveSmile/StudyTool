import uuid
import json
import pytest
from app.database import db_session
from app.routers.analysis import (
    list_knowledge_points, delete_knowledge_point, restore_knowledge_point,
    get_legacy_knowledge_points, merge_knowledge_points
)
from app.models.schemas import KnowledgePointMergeRequest
from app.services.stats_engine import StatsEngine
from app.routers.practice import list_practice_sessions
from fastapi import HTTPException

def test_soft_delete_and_restore():
    kp_id = str(uuid.uuid4())
    with db_session() as conn:
        conn.execute("""
            INSERT INTO knowledge_points (id, name, chapter, priority_tag, is_deleted)
            VALUES (?, '测试考点', '第一章', 'normal', 0)
        """, (kp_id,))

    # Active in list
    points = list_knowledge_points()
    assert any(p.id == kp_id for p in points)

    # Soft delete
    del_res = delete_knowledge_point(kp_id)
    assert del_res["status"] == "ok"

    # Excluded from list
    points_after = list_knowledge_points()
    assert not any(p.id == kp_id for p in points_after)

    # Excluded from stats
    stats = StatsEngine.get_knowledge_points_stats()
    assert not any(s.id == kp_id for s in stats)

    # Check row still exists in DB with is_deleted=1
    with db_session() as conn:
        row = conn.execute("SELECT is_deleted, deleted_at FROM knowledge_points WHERE id = ?", (kp_id,)).fetchone()
        assert row["is_deleted"] == 1
        assert row["deleted_at"] is not None

    # Restore
    rest_res = restore_knowledge_point(kp_id)
    assert rest_res["status"] == "ok"
    points_restored = list_knowledge_points()
    assert any(p.id == kp_id for p in points_restored)

def test_legacy_knowledge_points():
    kp_id = str(uuid.uuid4())
    with db_session() as conn:
        conn.execute("""
            INSERT INTO knowledge_points (id, name, source_type, is_deleted)
            VALUES (?, '旧考点', 'pending_verification', 0)
        """, (kp_id,))

    leg = get_legacy_knowledge_points()
    assert any(p.id == kp_id for p in leg)

    # If soft-deleted, excluded from legacy list
    delete_knowledge_point(kp_id)
    leg_after = get_legacy_knowledge_points()
    assert not any(p.id == kp_id for p in leg_after)

def test_merge_preserves_practice_sessions():
    target_id = str(uuid.uuid4())
    source_id = str(uuid.uuid4())
    with db_session() as conn:
        conn.execute("INSERT INTO knowledge_points (id, name, is_deleted) VALUES (?, '目标考点', 0)", (target_id,))
        conn.execute("INSERT INTO knowledge_points (id, name, is_deleted) VALUES (?, '源考点', 0)", (source_id,))
        # Insert a practice session linked to source_id
        session_id = str(uuid.uuid4())
        conn.execute("""
            INSERT INTO practice_sessions (id, knowledge_point_id, question_count, difficulty, questions_json)
            VALUES (?, ?, 1, 'basic', ?)
        """, (session_id, source_id, json.dumps([{"question_id": "q1", "stem": "测试题"}])))

    # Merge source into target
    merge_res = merge_knowledge_points(KnowledgePointMergeRequest(target_id=target_id, source_ids=[source_id]))
    assert merge_res["status"] == "ok"

    # Practice session still exists and is reassigned to target_id
    with db_session() as conn:
        ps = conn.execute("SELECT knowledge_point_id FROM practice_sessions WHERE id = ?", (session_id,)).fetchone()
        assert ps is not None
        assert ps["knowledge_point_id"] == target_id

    # List practice sessions returns valid record with target kp name
    sessions = list_practice_sessions()
    matching = [s for s in sessions if s.id == session_id]
    assert len(matching) == 1
    assert matching[0].knowledge_point_name == "目标考点"
