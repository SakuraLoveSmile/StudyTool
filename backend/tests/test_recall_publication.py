"""Atomic review/publication and optimistic-concurrency regression coverage."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.database import get_db_connection
from app.main import app
from app.services import recall_cards
from app.services.learning_service import LearningService
from app.services.recall_cards import RecallCardsError, RecallCardsService as Cards


@pytest.fixture
def conn():
    connection = get_db_connection()
    try:
        yield connection
    finally:
        connection.close()


def _draft(conn):
    chapters = Cards.list_chapters(conn=conn)
    chapter = chapters[0] if chapters else Cards.create_chapter({"title": "章节"}, conn=conn)
    return Cards.create_card(
        {"chapter_id": chapter["id"], "prompt": "问题", "answer": "答案"}, conn=conn
    )


def _approve(conn, card):
    Cards.review_revisions({"revision_id": card["revision_id"], "decision": "approve"}, conn=conn)
    return Cards.get_card(card["id"], conn=conn)


def _snapshot(conn):
    return {
        table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
        for table in ("recall_cards", "recall_card_revisions", "learning_progress", "learning_events")
    }


def test_atomic_publication_api_accepts_original_card_version(conn):
    card = _draft(conn)
    with TestClient(app) as client:
        response = client.post(f"/api/learning/cards/{card['id']}/review-and-publish", json={
            "revision_id": card["revision_id"], "expected_version": card["version"],
        })
    assert response.status_code == 200, response.text
    published = response.json()
    assert published["lifecycle_status"] == "published"
    assert published["published_revision_id"] == card["revision_id"]
    assert published["current_revision_status"] == "published"
    assert published["has_unpublished_changes"] is False
    assert published["version"] > card["version"]


@pytest.mark.parametrize("version_fields,expected_status", [({}, 200), ({"version": 1}, 200), ({"expected_version": 0}, 422)])
def test_atomic_publication_api_preserves_optional_version_contract(conn, version_fields, expected_status):
    card = _draft(conn)
    with TestClient(app) as client:
        response = client.post(f"/api/learning/cards/{card['id']}/review-and-publish", json={
            "revision_id": card["revision_id"], **version_fields,
        })
    assert response.status_code == expected_status, response.text


@pytest.mark.parametrize("review_first", [False, True])
def test_atomic_publication_supports_draft_and_approved_recovery(conn, review_first):
    card = _draft(conn)
    if review_first:
        card = _approve(conn, card)
    published = Cards.approve_and_publish_card(card["id"], revision_id=card["revision_id"],
        expected_version=card["version"], conn=conn)
    assert published["published_revision_id"] == card["revision_id"]
    assert conn.execute("SELECT COUNT(*) FROM learning_events WHERE event_type = 'revision_approved'").fetchone()[0] == 1


def test_atomic_publication_can_approve_rejected_revision(conn):
    card = _draft(conn)
    Cards.review_revisions({"revision_id": card["revision_id"], "decision": "reject"}, conn=conn)
    published = Cards.approve_and_publish_card(card["id"], revision_id=card["revision_id"], conn=conn)
    assert published["current_revision_status"] == "published"


def test_atomic_publication_rejects_real_stale_version_without_writes(conn):
    card = _draft(conn)
    _approve(conn, card)
    before = _snapshot(conn)
    with TestClient(app) as client:
        response = client.post(f"/api/learning/cards/{card['id']}/review-and-publish", json={
            "revision_id": card["revision_id"], "expected_version": card["version"],
        })
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "VERSION_CONFLICT"
    assert _snapshot(conn) == before


def test_atomic_publication_rejects_revision_from_other_card(conn):
    card, other = _draft(conn), _draft(conn)
    before = _snapshot(conn)
    with pytest.raises(RecallCardsError) as error:
        Cards.approve_and_publish_card(card["id"], revision_id=other["revision_id"], conn=conn)
    assert error.value.code == "REVISION_NOT_FOUND"
    assert _snapshot(conn) == before


@pytest.mark.parametrize("publish_first", [False, True])
def test_atomic_publication_rejects_old_revision_even_with_fresh_version(conn, publish_first):
    card = _draft(conn)
    if publish_first:
        card = Cards.approve_and_publish_card(card["id"], conn=conn)
    old_revision_id = card["revision_id"]
    Cards.create_revision(card["id"], {"prompt": "新版问题", "answer": "新版答案"}, conn=conn)
    current = Cards.get_card(card["id"], conn=conn)
    before = _snapshot(conn)
    with pytest.raises(RecallCardsError) as error:
        Cards.approve_and_publish_card(card["id"], revision_id=old_revision_id,
            expected_version=current["version"], conn=conn)
    assert error.value.code == "CURRENT_REVISION_CHANGED"
    assert _snapshot(conn) == before


def test_published_current_revision_is_noop_but_stale_version_still_conflicts(conn):
    card = Cards.approve_and_publish_card(_draft(conn)["id"], conn=conn)
    before = _snapshot(conn)
    again = Cards.approve_and_publish_card(card["id"], revision_id=card["revision_id"],
        expected_version=card["version"], conn=conn)
    assert again == card
    assert _snapshot(conn) == before
    with pytest.raises(RecallCardsError) as error:
        Cards.approve_and_publish_card(card["id"], expected_version=card["version"] - 1, conn=conn)
    assert error.value.code == "VERSION_CONFLICT"


def test_publication_failure_rolls_back_review_version_events_and_progress(conn, monkeypatch):
    card = Cards.approve_and_publish_card(_draft(conn)["id"], conn=conn)
    Cards.create_revision(card["id"], {"prompt": "新版问题", "answer": "新版答案"}, conn=conn)
    before = _snapshot(conn)
    original_log = recall_cards._log_event

    def fail_after_publication(db, event_type, *args, **kwargs):
        original_log(db, event_type, *args, **kwargs)
        if event_type == "card_published":
            raise RuntimeError("simulated publication persistence failure")

    monkeypatch.setattr(recall_cards, "_log_event", fail_after_publication)
    with pytest.raises(RuntimeError, match="simulated publication"):
        Cards.approve_and_publish_card(card["id"], reset_progress=True, conn=conn)
    assert _snapshot(conn) == before


def test_new_revision_publication_preserves_study_progress_by_default(conn):
    card = Cards.approve_and_publish_card(_draft(conn)["id"], conn=conn)
    conn.execute("UPDATE learning_progress SET review_count=7, interval_index=3, state='review', due_date='2030-01-01' WHERE card_id=?", (card["id"],))
    conn.commit()
    progress = dict(conn.execute("SELECT * FROM learning_progress WHERE card_id=?", (card["id"],)).fetchone())
    Cards.create_revision(card["id"], {"prompt": "新版问题", "answer": "新版答案"}, conn=conn)
    pending = Cards.get_card(card["id"], include_candidate=False, conn=conn)
    assert pending["revision_id"] == card["published_revision_id"]
    assert pending["current_revision_id"] != pending["revision_id"]
    assert pending["current_revision_status"] == "draft"
    assert pending["has_unpublished_changes"] is True
    published = Cards.approve_and_publish_card(card["id"], revision_id=pending["current_revision_id"],
        expected_version=pending["version"], conn=conn)
    assert published["prompt"] == "新版问题"
    assert published["has_unpublished_changes"] is False
    assert dict(conn.execute("SELECT * FROM learning_progress WHERE card_id=?", (card["id"],)).fetchone()) == progress


def test_draft_and_rejected_revisions_do_not_masquerade_as_published_changes(conn):
    card = _draft(conn)
    assert card["current_revision_id"] == card["revision_id"]
    assert card["current_revision_status"] == "draft"
    assert card["has_unpublished_changes"] is False
    card = Cards.approve_and_publish_card(card["id"], conn=conn)
    revision = Cards.create_revision(card["id"], {"prompt": "拒绝问题", "answer": "答案"}, conn=conn)
    Cards.review_revisions({"revision_id": revision["id"], "decision": "reject"}, conn=conn)
    assert Cards.get_card(card["id"], conn=conn)["has_unpublished_changes"] is False


def test_explicit_progress_reset_is_forwarded_to_publication(conn):
    card = Cards.approve_and_publish_card(_draft(conn)["id"], conn=conn)
    conn.execute("UPDATE learning_progress SET review_count=7, interval_index=3, state='review', due_date='2030-01-01' WHERE card_id=?", (card["id"],))
    conn.commit()
    Cards.create_revision(card["id"], {"prompt": "新版问题", "answer": "新版答案"}, conn=conn)
    Cards.approve_and_publish_card(card["id"], reset_progress=True, conn=conn)
    progress = dict(conn.execute("SELECT * FROM learning_progress WHERE card_id=?", (card["id"],)).fetchone())
    assert progress["review_count"] == 0
    assert progress["interval_index"] == 0
    assert progress["state"] == "new"
    assert progress["due_date"] is None
    assert progress["reset_at"]


@pytest.mark.parametrize("lifecycle", ["published", "paused", "archived"])
def test_rejecting_new_draft_preserves_published_content_lifecycle_and_daily_eligibility(conn, lifecycle):
    card = Cards.approve_and_publish_card(_draft(conn)["id"], conn=conn)
    if lifecycle != "published":
        Cards.set_card_lifecycle(card["id"], lifecycle, conn=conn)
    revision = Cards.create_revision(card["id"], {"prompt": "待拒绝的新问题", "answer": "待拒绝的新答案"}, conn=conn)
    before = dict(conn.execute("SELECT * FROM recall_cards WHERE id=?", (card["id"],)).fetchone())
    progress = dict(conn.execute("SELECT * FROM learning_progress WHERE card_id=?", (card["id"],)).fetchone())
    before_candidates = LearningService._card_candidates(conn, {}, False)
    assert bool(before_candidates) is (lifecycle == "published")

    Cards.review_revisions({"revision_id": revision["id"], "decision": "reject",
        "expected_card_version": before["lock_version"]}, conn=conn)

    after = dict(conn.execute("SELECT * FROM recall_cards WHERE id=?", (card["id"],)).fetchone())
    assert after["version"] == before["version"] + 1
    assert after["lock_version"] == before["lock_version"] + 1
    for key in before.keys() - {"version", "lock_version", "updated_at"}:
        assert after[key] == before[key], key
    published = Cards.get_card(card["id"], include_candidate=False, conn=conn)
    assert published["prompt"] == card["prompt"]
    assert published["answer"] == card["answer"]
    assert published["revision_id"] == card["published_revision_id"]
    assert published["current_revision_status"] == "rejected"
    assert published["has_unpublished_changes"] is False
    candidates = LearningService._card_candidates(conn, {}, False)
    assert [(row["card_id"], row["revision_id"]) for row in candidates] == [
        (row["card_id"], row["revision_id"]) for row in before_candidates
    ]
    assert dict(conn.execute("SELECT * FROM learning_progress WHERE card_id=?", (card["id"],)).fetchone()) == progress


def test_rejecting_unpublished_candidate_retains_existing_card_rejection(conn):
    card = _draft(conn)
    Cards.review_revisions({"revision_id": card["revision_id"], "decision": "reject"}, conn=conn)
    row = dict(conn.execute("SELECT * FROM recall_cards WHERE id=?", (card["id"],)).fetchone())
    assert row["publication_status"] == "rejected"
    assert row["status"] == "draft"
    assert row["published_revision_id"] is None
    assert LearningService._card_candidates(conn, {}, False) == []
