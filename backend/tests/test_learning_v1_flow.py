"""Focused coverage for the V1 recall-card and daily-learning contract."""
from __future__ import annotations

import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_db_connection, init_db
from app.main import app
from app.services.learning_service import LearningService, LearningServiceError
from app.services.recall_cards import RecallCardsService


def _published_card(conn: sqlite3.Connection, *, prompt: str = "问题", answer: str = "答案") -> dict:
    chapter = RecallCardsService.create_chapter({"title": "章节"}, conn=conn)
    card = RecallCardsService.create_card(
        {"chapter_id": chapter["id"], "prompt": prompt, "answer": answer}, conn=conn
    )
    reviewed = RecallCardsService.review_revisions(
        {"revisions": [{"revision_id": card["revision_id"], "decision": "approve"}]}, conn=conn
    )
    assert isinstance(reviewed, dict)
    return RecallCardsService.publish_card(card["id"], revision_id=card["revision_id"], conn=conn)


def test_fresh_schema_is_v6_and_init_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    init_db()
    conn = get_db_connection()
    try:
        assert conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()[0] == "6"
        for table in ("recall_chapters", "recall_cards", "recall_card_revisions", "learning_progress", "learning_days", "learning_day_units", "learning_attempts", "learning_events"):
            assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    finally:
        conn.close()
    init_db()


def test_published_card_daily_flow_rating_replay_and_attempt_route(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    init_db()
    conn = get_db_connection()
    try:
        settings_result = LearningService.update_settings(
            {"daily_new_limit": 5, "daily_review_limit": 5, "timezone": "Asia/Shanghai"}, conn=conn
        )
        assert settings_result["daily_new_limit"] == 5
        card = _published_card(conn)
        opened = LearningService.open_today(conn=conn)
        attempt_id = opened["next_item"]["attempt_id"]
        LearningService.reveal(conn=conn, attempt_id=attempt_id)
        rated = LearningService.rate(conn=conn, attempt_id=attempt_id, rating="mastered", request_id="req-1")
        assert rated["rating"] == "mastered"
        assert rated["progress"]["interval_index"] == 0
        replay = LearningService.rate(conn=conn, attempt_id=attempt_id, rating="mastered", request_id="req-1")
        assert replay["idempotent_replay"] is True
        with pytest.raises(LearningServiceError) as conflict:
            LearningService.rate(conn=conn, attempt_id=attempt_id, rating="fuzzy", request_id="req-2")
        assert conflict.value.code in {"ATTEMPT_NOT_FOUND", "ATTEMPT_ALREADY_RATED"}
        assert conn.execute("SELECT COUNT(*) FROM learning_events WHERE request_id = 'req-1'").fetchone()[0] == 1
        assert card["id"] == opened["next_item"]["card_id"]
    finally:
        conn.close()


def test_learning_api_settings_content_and_stale_version(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    init_db()
    with TestClient(app) as client:
        settings_response = client.put("/api/learning/settings", json={"daily_new_limit": 3, "timezone": "Asia/Shanghai"})
        assert settings_response.status_code == 200
        assert settings_response.json()["daily_new_limit"] == 3
        chapter_response = client.post("/api/learning/chapters", json={"title": "API 章节"})
        assert chapter_response.status_code == 200
        chapter_id = chapter_response.json()["id"]
        card_response = client.post("/api/learning/cards", json={"chapter_id": chapter_id, "prompt": "Q", "answer": "A"})
        assert card_response.status_code == 200
        card = card_response.json()
        update_response = client.patch(f"/api/learning/cards/{card['id']}", json={"prompt": "Q2", "answer": "A2"})
        assert update_response.status_code == 200
        stale_response = client.patch(f"/api/learning/cards/{card['id']}", json={"version": 1, "prompt": "stale", "answer": "stale"})
        assert stale_response.status_code == 409
        assert stale_response.json()["detail"]["code"] == "VERSION_CONFLICT"


def test_explicit_settings_reject_zero_and_invalid_timezone(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    init_db()
    conn = get_db_connection()
    try:
        with pytest.raises(LearningServiceError) as zero:
            LearningService.update_settings({"daily_new_limit": 0}, conn=conn)
        assert zero.value.code == "INVALID_REQUEST"
        with pytest.raises(LearningServiceError) as tz:
            LearningService.update_settings({"daily_new_limit": 1, "timezone": "UTC"}, conn=conn)
        assert tz.value.code == "INVALID_TIMEZONE"
    finally:
        conn.close()
