"""Regression coverage for the fixed rules of the V1 daily learning loop.

These tests pin the behaviours that earlier audits found missing:

* due reviews / unfinished carryover block new cards unless the user opts into
  the explicit one-day override;
* ``daily_review_limit`` caps ordinary due reviews but never hides carryover;
* same-day additional recall is schedule neutral;
* pause/archive/source invalidation turn an already built day unit into a
  non-actionable qualification instead of a learnable state;
* manual (or AI) cards only reach the daily queue after human review + publish;
* live daily routes reject a stale ``study_date`` with ``DAY_CHANGED``.
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_db_connection, init_db
from app.main import app
from app.services.learning_service import LearningService, LearningServiceError
from app.services.recall_cards import RecallCardsService

DAY_ONE = "2026-03-02"
DAY_TWO = "2026-03-03"
DAY_THREE = "2026-03-04"


def _chapter(conn: sqlite3.Connection, title: str = "章节") -> dict:
    return RecallCardsService.create_chapter({"title": title}, conn=conn)


def _published_card(conn: sqlite3.Connection, chapter_id: str, prompt: str, answer: str) -> dict:
    card = RecallCardsService.create_card(
        {"chapter_id": chapter_id, "prompt": prompt, "answer": answer}, conn=conn
    )
    RecallCardsService.review_revisions(
        {"revisions": [{"revision_id": card["revision_id"], "decision": "approve"}]}, conn=conn
    )
    return RecallCardsService.publish_card(card["id"], revision_id=card["revision_id"], conn=conn)


def _finish_round(conn: sqlite3.Connection, study_date: str, token: str, rating: str) -> dict:
    """Rate the current item of a round, revealing it first."""
    opened = LearningService.get_today(conn=conn, study_date=study_date)
    item = opened.get("next_item") or {}
    if not item or item.get("done"):
        return {}
    attempt_id = item.get("attempt_id") or (item.get("attempt") or {}).get("id")
    revealed = LearningService.reveal(conn=conn, study_date=study_date, attempt_id=attempt_id)
    assert revealed.get("answer") is not None
    return LearningService.rate(
        conn=conn,
        study_date=study_date,
        attempt_id=attempt_id,
        rating=rating,
        request_id=f"{token}-{attempt_id}",
    )


def _drain_day(conn: sqlite3.Connection, study_date: str, rating: str, token: str, limit: int = 20) -> list[dict]:
    results = []
    for index in range(limit):
        rated = _finish_round(conn, study_date, f"{token}-{index}", rating)
        if not rated:
            break
        results.append(rated)
    return results


def test_due_review_and_carryover_block_new_cards_until_override(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    init_db()
    conn = get_db_connection()
    try:
        LearningService.update_settings(
            {"daily_new_limit": 5, "daily_review_limit": 5, "timezone": "Asia/Shanghai"}, conn=conn
        )
        chapter = _chapter(conn)
        _published_card(conn, chapter["id"], "卡片A", "答案A")
        _published_card(conn, chapter["id"], "卡片B", "答案B")

        # Day one: only the first new card should be queued under limit 1.
        LearningService.update_settings({"daily_new_limit": 1}, conn=conn)
        LearningService.open_today(conn=conn, study_date=DAY_ONE)
        day_one = LearningService.get_today(conn=conn, study_date=DAY_ONE)
        assert day_one["counts"]["total"] == 1
        assert day_one["counts"]["new"] == 1

        # Finish the three same-day rounds so the card becomes a due review.
        _drain_day(conn, DAY_ONE, "mastered", "d1")
        for round_number in (2, 3):
            assert LearningService.advance(conn=conn, study_date=DAY_ONE)["current_round"] == round_number
            _drain_day(conn, DAY_ONE, "mastered", f"d1-r{round_number}")

        # Day two: the finished card is a due review at +1 day and it must block
        # the untouched second card from entering new learning.
        LearningService.open_today(conn=conn, study_date=DAY_TWO)
        day_two = LearningService.get_today(conn=conn, study_date=DAY_TWO)
        assert day_two["counts"]["review"] >= 1
        assert day_two["counts"]["new"] == 0, "due review must block new cards by default"

        # The explicit one-day override is the only way to opt into new cards.
        LearningService.override(conn=conn, study_date=DAY_TWO, enabled=True)
        day_two_override = LearningService.get_today(conn=conn, study_date=DAY_TWO)
        assert day_two_override["counts"]["new"] >= 1
        assert day_two_override["counts"]["review"] >= 1
    finally:
        conn.close()


def test_daily_review_limit_caps_due_reviews_without_hiding_carryover(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    init_db()
    conn = get_db_connection()
    try:
        LearningService.update_settings(
            {"daily_new_limit": 3, "daily_review_limit": 10, "timezone": "Asia/Shanghai"}, conn=conn
        )
        chapter = _chapter(conn)
        for name in ("甲", "乙", "丙"):
            _published_card(conn, chapter["id"], f"卡片{name}", f"答案{name}")

        LearningService.open_today(conn=conn, study_date=DAY_ONE)
        _drain_day(conn, DAY_ONE, "mastered", "d1")
        for round_number in (2, 3):
            LearningService.advance(conn=conn, study_date=DAY_ONE)
            _drain_day(conn, DAY_ONE, "mastered", f"d1-r{round_number}")

        # Three cards are now due at +1 day, but only one may be reviewed today.
        LearningService.update_settings({"daily_review_limit": 1}, conn=conn)
        LearningService.open_today(conn=conn, study_date=DAY_TWO)
        limited = LearningService.get_today(conn=conn, study_date=DAY_TWO)
        assert limited["counts"]["review"] == 1, limited["counts"]
        assert limited["counts"]["new"] == 0

        # Overriding is an explicit user decision that lifts both gates.
        LearningService.override(conn=conn, study_date=DAY_TWO, enabled=True)
        unlimited = LearningService.get_today(conn=conn, study_date=DAY_TWO)
        assert unlimited["counts"]["review"] == 3
    finally:
        conn.close()


def test_same_day_additional_recall_never_advances_intervals(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    init_db()
    conn = get_db_connection()
    try:
        LearningService.update_settings(
            {"daily_new_limit": 2, "daily_review_limit": 2, "timezone": "Asia/Shanghai"}, conn=conn
        )
        chapter = _chapter(conn)
        _published_card(conn, chapter["id"], "卡片", "答案")

        LearningService.open_today(conn=conn, study_date=DAY_ONE)
        first = _finish_round(conn, DAY_ONE, "add", "forgotten")
        primary = first["progress"]
        assert primary["interval_index"] == 0
        assert primary["due_date"] == DAY_TWO

        additional = first.get("additional_attempt")
        assert additional is not None, "a forgotten primary rating must create an additional attempt"
        LearningService.reveal(conn=conn, study_date=DAY_ONE, attempt_id=additional["id"])
        retry = LearningService.rate(
            conn=conn,
            study_date=DAY_ONE,
            attempt_id=additional["id"],
            rating="mastered",
            request_id="add-retry",
        )
        after = retry["progress"]
        assert after["interval_index"] == primary["interval_index"], "same-day recap must not advance the ladder"
        assert after["interval_days"] == primary["interval_days"]
        assert after["due_date"] == primary["due_date"], "same-day recap must not move the due date"
    finally:
        conn.close()


def test_paused_and_source_invalid_units_stop_being_actionable(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    init_db()
    conn = get_db_connection()
    try:
        LearningService.update_settings(
            {"daily_new_limit": 2, "daily_review_limit": 2, "timezone": "Asia/Shanghai"}, conn=conn
        )
        chapter = _chapter(conn)
        card = _published_card(conn, chapter["id"], "卡片", "答案")
        opened = LearningService.open_today(conn=conn, study_date=DAY_ONE)
        attempt_id = opened["next_item"]["attempt_id"]

        RecallCardsService.set_card_lifecycle(card["id"], status="paused", conn=conn)
        with pytest.raises(LearningServiceError) as paused:
            LearningService.reveal(conn=conn, study_date=DAY_ONE, attempt_id=attempt_id)
        assert paused.value.code == "CARD_PAUSED"
        with pytest.raises(LearningServiceError) as paused_rate:
            LearningService.rate(
                conn=conn, study_date=DAY_ONE, attempt_id=attempt_id, rating="mastered", request_id="paused-rate"
            )
        assert paused_rate.value.code == "CARD_PAUSED"

        RecallCardsService.set_card_lifecycle(card["id"], status="published", conn=conn)
        assert LearningService.reveal(conn=conn, study_date=DAY_ONE, attempt_id=attempt_id)["answer"]

        # Source invalidation is a qualification too: the same unit must stop.
        conn.execute("UPDATE recall_card_revisions SET source_status = 'invalid' WHERE card_id = ?", (card["id"],))
        conn.commit()
        with pytest.raises(LearningServiceError) as invalid:
            LearningService.reveal(conn=conn, study_date=DAY_ONE, attempt_id=attempt_id)
        assert invalid.value.code == "SOURCE_INVALID"
    finally:
        conn.close()


def test_manual_card_reaches_today_only_after_publish(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    init_db()
    conn = get_db_connection()
    try:
        LearningService.update_settings(
            {"daily_new_limit": 5, "daily_review_limit": 5, "timezone": "Asia/Shanghai"}, conn=conn
        )
        chapter = _chapter(conn)
        draft = RecallCardsService.create_card(
            {"chapter_id": chapter["id"], "prompt": "草稿", "answer": "草稿答案"}, conn=conn
        )
        LearningService.open_today(conn=conn, study_date=DAY_ONE)
        assert LearningService.get_today(conn=conn, study_date=DAY_ONE)["counts"]["total"] == 0

        RecallCardsService.review_revisions(
            {"revisions": [{"revision_id": draft["revision_id"], "decision": "approve"}]}, conn=conn
        )
        RecallCardsService.publish_card(draft["id"], revision_id=draft["revision_id"], conn=conn)
        LearningService.override(conn=conn, study_date=DAY_ONE, enabled=True)
        assert LearningService.get_today(conn=conn, study_date=DAY_ONE)["counts"]["total"] == 1
    finally:
        conn.close()


def test_settings_persist_ui_fields_and_reject_stale_version(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    init_db()
    conn = get_db_connection()
    try:
        created = LearningService.update_settings(
            {
                "daily_new_limit": 4,
                "daily_review_limit": 2,
                "include_unassigned": False,
                "auto_advance": True,
                "show_hints": False,
                "reveal_after_seconds": 15,
                "timezone": "Asia/Shanghai",
            },
            conn=conn,
        )
        assert created["include_unassigned"] is False
        assert created["auto_advance"] is True
        assert created["show_hints"] is False
        assert created["reveal_after_seconds"] == 15

        reloaded = LearningService.get_settings(conn=conn)
        assert reloaded["auto_advance"] is True
        assert reloaded["show_hints"] is False
        assert reloaded["reveal_after_seconds"] == 15
        assert reloaded["include_unassigned"] is False

        with pytest.raises(LearningServiceError) as conflict:
            LearningService.update_settings(
                {"daily_new_limit": 9, "expected_version": int(created["version"]) + 1}, conn=conn
            )
        assert conflict.value.code == "VERSION_CONFLICT"
    finally:
        conn.close()


def test_live_daily_routes_reject_stale_study_date(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    init_db()
    with TestClient(app) as client:
        stale = client.get("/api/learning/today", params={"study_date": "2020-01-01"})
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "DAY_CHANGED"
        stale_open = client.post("/api/learning/today/open", json={"study_date": "2020-01-01"})
        assert stale_open.status_code == 409
        assert stale_open.json()["detail"]["code"] == "DAY_CHANGED"


def test_daily_attempt_envelope_hides_answer_until_reveal(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    init_db()
    with TestClient(app) as client:
        assert client.put(
            "/api/learning/settings", json={"daily_new_limit": 3, "timezone": "Asia/Shanghai"}
        ).status_code == 200
        chapter_id = client.post("/api/learning/chapters", json={"title": "接口章节"}).json()["id"]
        card = client.post(
            "/api/learning/cards", json={"chapter_id": chapter_id, "prompt": "接口问题", "answer": "接口答案"}
        ).json()
        client.post(
            "/api/learning/revisions/review",
            json={"revisions": [{"revision_id": card["revision_id"], "decision": "approve"}]},
        )
        publish = client.post(f"/api/learning/cards/{card['id']}/publish", json={"reset_progress": False})
        assert publish.status_code == 200

        opened = client.post("/api/learning/today/open", json={})
        assert opened.status_code == 200
        summary = opened.json()
        current = summary.get("next_item") or summary.get("current_item") or {}
        attempt_id = current.get("attempt_id") or (current.get("attempt") or {}).get("id")
        assert attempt_id, summary

        before = current.get("question") or ""
        assert "答案" not in str(current.get("answer") or "")
        revealed = client.post(f"/api/learning/attempts/{attempt_id}/reveal", json={})
        assert revealed.status_code == 200
        assert revealed.json()["answer"]

        rated = client.post(
            f"/api/learning/attempts/{attempt_id}/rate",
            json={"rating": "mastered", "request_id": "http-rate-1"},
        )
        assert rated.status_code == 200
        assert rated.json()["rating"] == "mastered"
        replay = client.post(
            f"/api/learning/attempts/{attempt_id}/rate",
            json={"rating": "mastered", "request_id": "http-rate-1"},
        )
        assert replay.status_code == 200
        assert replay.json()["idempotent_replay"] is True


def test_generation_collection_and_card_lifecycle_routes_exist(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    init_db()
    with TestClient(app) as client:
        generations = client.get("/api/learning/generations")
        assert generations.status_code == 200
        assert isinstance(generations.json(), list)

        chapter_id = client.post("/api/learning/chapters", json={"title": "生命周期章节"}).json()["id"]
        card = client.post(
            "/api/learning/cards", json={"chapter_id": chapter_id, "prompt": "Q", "answer": "A"}
        ).json()
        paused = client.post(f"/api/learning/cards/{card['id']}/lifecycle", json={"status": "paused"})
        assert paused.status_code == 200
        restored = client.post(f"/api/learning/cards/{card['id']}/lifecycle", json={"status": "published"})
        assert restored.status_code == 200


def test_republishing_with_reset_progress_clears_the_whole_schedule(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    init_db()
    conn = get_db_connection()
    try:
        LearningService.update_settings(
            {"daily_new_limit": 2, "daily_review_limit": 2, "timezone": "Asia/Shanghai"}, conn=conn
        )
        chapter = _chapter(conn)
        card = _published_card(conn, chapter["id"], "卡片", "答案")
        LearningService.open_today(conn=conn, study_date=DAY_ONE)
        _drain_day(conn, DAY_ONE, "mastered", "reset")
        for round_number in (2, 3):
            LearningService.advance(conn=conn, study_date=DAY_ONE)
            _drain_day(conn, DAY_ONE, "mastered", f"reset-r{round_number}")

        studied = conn.execute(
            "SELECT interval_index, repetitions, review_count, due_date FROM learning_progress WHERE card_id = ?",
            (card["id"],),
        ).fetchone()
        assert studied["repetitions"] >= 3
        assert studied["due_date"] is not None

        revision = RecallCardsService.create_revision(
            card["id"], {"prompt": "卡片v2", "answer": "答案v2"}, conn=conn
        )
        RecallCardsService.review_revisions(
            {"revisions": [{"revision_id": revision["id"], "decision": "approve"}]}, conn=conn
        )
        RecallCardsService.publish_card(
            card["id"], revision_id=revision["id"], reset_progress=True, conn=conn
        )

        after = conn.execute(
            """
            SELECT content_revision_no, state, interval_index, repetitions, interval_days,
                   review_count, due_date, due_at, first_study_date, last_studied_at,
                   last_reviewed_at, reset_at
            FROM learning_progress WHERE card_id = ?
            """,
            (card["id"],),
        ).fetchone()
        assert after["state"] == "new"
        assert after["interval_index"] == 0
        assert after["interval_days"] == 0
        assert after["repetitions"] == 0
        assert after["review_count"] == 0
        assert after["due_date"] is None
        assert after["due_at"] is None
        assert after["first_study_date"] is None
        assert after["last_studied_at"] is None
        assert after["last_reviewed_at"] is None
        assert after["reset_at"], "a reset must be recorded on the progress row"

        # History stays readable after a reset.
        history = RecallCardsService.history(card["id"], conn=conn)
        assert any(item.get("id") == revision["id"] for item in history if isinstance(item, dict))
    finally:
        conn.close()


def test_api_timestamps_are_timezone_aware_utc(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    init_db()
    conn = get_db_connection()
    try:
        created = LearningService.update_settings(
            {"daily_new_limit": 2, "timezone": "Asia/Shanghai"}, conn=conn
        )
        assert created["updated_at"].endswith("+00:00"), created["updated_at"]

        chapter = _chapter(conn)
        card = _published_card(conn, chapter["id"], "卡片", "答案")
        assert str(card["created_at"]).endswith("+00:00"), card["created_at"]

        opened = LearningService.open_today(conn=conn, study_date=DAY_ONE)
        attempt_id = opened["next_item"]["attempt_id"]
        LearningService.reveal(conn=conn, study_date=DAY_ONE, attempt_id=attempt_id)
        rated = LearningService.rate(
            conn=conn, study_date=DAY_ONE, attempt_id=attempt_id, rating="mastered", request_id="utc-1"
        )
        assert rated["attempt"]["submitted_at"].endswith("+00:00")
        assert rated["attempt"]["revealed_at"].endswith("+00:00")
    finally:
        conn.close()


def test_candidate_listing_and_approval_over_http(tmp_path, monkeypatch):
    """Candidates stay out of Today until a human approves and publishes them."""
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    init_db()
    with TestClient(app) as client:
        assert client.put(
            "/api/learning/settings", json={"daily_new_limit": 3, "timezone": "Asia/Shanghai"}
        ).status_code == 200
        chapter_id = client.post("/api/learning/chapters", json={"title": "候选章节"}).json()["id"]
        card = client.post(
            "/api/learning/cards", json={"chapter_id": chapter_id, "prompt": "候选问题", "answer": "候选答案"}
        ).json()

        default_list = client.get("/api/learning/cards")
        assert default_list.status_code == 200
        assert default_list.json() == []

        with_candidates = client.get("/api/learning/cards", params={"include_candidates": True})
        assert with_candidates.status_code == 200
        listed = with_candidates.json()
        assert [item["id"] for item in listed] == [card["id"]]
        assert listed[0]["lifecycle_status"] == "draft"
        assert listed[0]["is_confirmed"] is False

        assert client.post("/api/learning/today/open", json={}).status_code == 200
        assert client.get("/api/learning/today").json()["counts"]["total"] == 0

        approve = client.post(
            "/api/learning/revisions/review",
            json={"revisions": [{"revision_id": card["revision_id"], "decision": "approve"}]},
        )
        assert approve.status_code == 200
        published = client.post(f"/api/learning/cards/{card['id']}/publish", json={})
        assert published.status_code == 200
        assert published.json()["lifecycle_status"] == "published"

        # Published content becomes learnable; the queue is rebuilt on override.
        assert client.post("/api/learning/today/override", json={"enabled": True}).status_code == 200
        assert client.get("/api/learning/today").json()["counts"]["total"] == 1


def test_daily_new_limit_has_no_hidden_default(tmp_path, monkeypatch):
    """Without an explicit 1..100 limit no new card may enter the queue."""
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    init_db()
    conn = get_db_connection()
    try:
        chapter = _chapter(conn)
        _published_card(conn, chapter["id"], "卡片", "答案")

        LearningService.open_today(conn=conn, study_date=DAY_ONE)
        unset = LearningService.get_today(conn=conn, study_date=DAY_ONE)
        assert unset["settings"]["daily_new_limit"] is None
        assert unset["counts"]["total"] == 0, "unset limit must not learn everything"

        for bad in (0, 101, -3):
            with pytest.raises(LearningServiceError) as error:
                LearningService.update_settings({"daily_new_limit": bad}, conn=conn)
            assert error.value.code in {"INVALID_REQUEST", "INVALID_DAILY_NEW_LIMIT"}
            assert error.value.status_code == 422

        assert LearningService.update_settings({"daily_new_limit": 1}, conn=conn)["daily_new_limit"] == 1
        LearningService.override(conn=conn, study_date=DAY_ONE, enabled=True)
        assert LearningService.get_today(conn=conn, study_date=DAY_ONE)["counts"]["total"] == 1
    finally:
        conn.close()


def test_advance_runs_three_rounds_then_schedules_the_one_day_interval(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    init_db()
    conn = get_db_connection()
    try:
        LearningService.update_settings(
            {"daily_new_limit": 2, "daily_review_limit": 2, "timezone": "Asia/Shanghai"}, conn=conn
        )
        chapter = _chapter(conn)
        _published_card(conn, chapter["id"], "卡片", "答案")
        LearningService.open_today(conn=conn, study_date=DAY_ONE)

        with pytest.raises(LearningServiceError) as incomplete:
            LearningService.advance(conn=conn, study_date=DAY_ONE)
        assert incomplete.value.code == "ROUND_NOT_COMPLETE"

        first = _finish_round(conn, DAY_ONE, "r1", "mastered")
        assert first["progress"]["status"] == "learning"
        assert first["progress"]["due_date"] == DAY_ONE, "rounds stay on the same day"

        assert LearningService.advance(conn=conn, study_date=DAY_ONE)["current_round"] == 2
        second = _finish_round(conn, DAY_ONE, "r2", "mastered")
        assert second["progress"]["status"] == "learning"
        assert second["progress"]["due_date"] == DAY_ONE

        assert LearningService.advance(conn=conn, study_date=DAY_ONE)["current_round"] == 3
        third = _finish_round(conn, DAY_ONE, "r3", "mastered")
        assert third["progress"]["status"] == "review"
        assert third["progress"]["interval_index"] == 0
        assert third["progress"]["due_date"] == DAY_TWO, "the third mastered round moves to +1 day"

        finished = LearningService.advance(conn=conn, study_date=DAY_ONE)
        assert finished["status"] == "completed"

        reopened = LearningService.open_today(conn=conn, study_date=DAY_TWO)
        assert reopened["counts"]["review"] == 1
        assert reopened["counts"]["new"] == 0
    finally:
        conn.close()


def test_unfinished_new_card_carries_over_and_reopen_preserves_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    init_db()
    conn = get_db_connection()
    try:
        LearningService.update_settings(
            {"daily_new_limit": 1, "daily_review_limit": 1, "timezone": "Asia/Shanghai"}, conn=conn
        )
        chapter = _chapter(conn)
        _published_card(conn, chapter["id"], "卡片", "答案")

        LearningService.open_today(conn=conn, study_date=DAY_ONE)
        first = _finish_round(conn, DAY_ONE, "carry", "mastered")
        assert first["progress"]["status"] == "learning"

        units_before = conn.execute(
            "SELECT COUNT(*) FROM learning_day_units WHERE study_date = ?", (DAY_ONE,)
        ).fetchone()[0]
        attempts_before = conn.execute(
            "SELECT COUNT(*) FROM learning_attempts WHERE study_date = ?", (DAY_ONE,)
        ).fetchone()[0]

        # Re-opening the same day (e.g. after closing the app) must restore the
        # queue without duplicating units or attempts.
        reopened = LearningService.open_today(conn=conn, study_date=DAY_ONE)
        assert reopened["counts"]["total"] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM learning_day_units WHERE study_date = ?", (DAY_ONE,)
        ).fetchone()[0] == units_before
        assert conn.execute(
            "SELECT COUNT(*) FROM learning_attempts WHERE study_date = ?", (DAY_ONE,)
        ).fetchone()[0] == attempts_before

        # The unfinished card is carried into the next day as carryover work and
        # unrated same-day attempts are never silently marked as mastered.
        LearningService.open_today(conn=conn, study_date=DAY_TWO)
        day_two = LearningService.get_today(conn=conn, study_date=DAY_TWO)
        assert day_two["counts"]["carryover"] == 1
        assert day_two["counts"]["new"] == 0
        assert day_two["counts"]["total"] == 1
        progress = conn.execute(
            "SELECT status, repetitions, interval_index FROM learning_progress WHERE card_id = (SELECT card_id FROM learning_day_units WHERE study_date = ? LIMIT 1)",
            (DAY_TWO,),
        ).fetchone()
        assert progress["status"] == "learning"
        assert progress["repetitions"] >= 1
    finally:
        conn.close()
