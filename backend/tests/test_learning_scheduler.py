from datetime import date

import pytest

from app.services.learning_scheduler import (
    INTERVAL_DAYS,
    STATUS_LEARNING,
    STATUS_REVIEW,
    calculate_next_due_date,
    complete_new_card,
    should_carryover,
    transition_review,
)


FIXED_STUDY_DATE = date(2026, 9, 10)


@pytest.mark.parametrize(
    ("index", "expected_index", "expected_due"),
    [
        (0, 1, date(2026, 9, 13)),
        (1, 2, date(2026, 9, 17)),
        (2, 3, date(2026, 9, 24)),
        (3, 4, date(2026, 10, 10)),
        # The final interval is capped; mastering it does not move past 30d.
        (4, 4, date(2026, 10, 10)),
    ],
)
def test_fixed_date_mastered_review_transitions(index, expected_index, expected_due):
    result = transition_review("mastered", index, FIXED_STUDY_DATE)

    assert result["index"] == expected_index
    assert result["interval_index"] == expected_index
    assert result["next_due_date"] == expected_due
    assert result["due_date"] == expected_due
    assert result["status"] == STATUS_REVIEW


@pytest.mark.parametrize(
    ("rating", "index", "expected_index", "expected_due"),
    [
        ("fuzzy", 3, 3, date(2026, 9, 11)),
        ("forgotten", 3, 0, date(2026, 9, 11)),
    ],
)
def test_fixed_date_fuzzy_and_forgotten_branches(
    rating, index, expected_index, expected_due
):
    result = transition_review(rating, index, FIXED_STUDY_DATE)

    assert result["index"] == expected_index
    assert result["next_due_date"] == expected_due
    assert result["rating"] == rating


def test_fixed_date_new_card_three_mastered_rounds_enters_first_review_interval():
    first = complete_new_card(0, FIXED_STUDY_DATE)
    second = complete_new_card(first, FIXED_STUDY_DATE)
    third = complete_new_card(second, FIXED_STUDY_DATE)

    assert first["rounds_completed"] == 1
    assert first["status"] == STATUS_LEARNING
    assert first["next_due_date"] == date(2026, 9, 11)

    assert second["rounds_completed"] == 2
    assert second["status"] == STATUS_LEARNING
    assert second["next_due_date"] == date(2026, 9, 11)

    assert third["rounds_completed"] == 3
    assert third["status"] == STATUS_REVIEW
    assert third["index"] == 0
    assert third["next_due_date"] == date(2026, 9, 11)


def test_new_card_first_forgotten_then_three_mastered_completes_on_next_day():
    forgotten = complete_new_card(0, FIXED_STUDY_DATE, rating="forgotten")
    # A forgotten primary still occupies the first primary slot; it does not
    # invalidate the later primary slots.
    assert forgotten["rounds_completed"] == 1
    assert forgotten["primary_rounds"] == ["forgotten", None, None]
    assert forgotten["status"] == STATUS_LEARNING
    assert forgotten["next_due_date"] == date(2026, 9, 11)

    # The retry is explicitly dated the next day. Three primary slots then
    # enter the ordinary one-day review interval from that retry date.
    first = complete_new_card(forgotten, date(2026, 9, 11))
    second = complete_new_card(first, date(2026, 9, 11))
    third = complete_new_card(second, date(2026, 9, 11))

    assert third["rounds_completed"] == 3
    assert third["status"] == STATUS_REVIEW
    assert third["index"] == 0
    assert third["next_due_date"] == date(2026, 9, 12)


def test_new_card_third_forgotten_then_additional_mastered_stays_carryover():
    first = complete_new_card(0, FIXED_STUDY_DATE)
    second = complete_new_card(first, FIXED_STUDY_DATE)
    third_forgotten = complete_new_card(
        second, FIXED_STUDY_DATE, rating="forgotten"
    )

    assert third_forgotten["rounds_completed"] == 3
    assert third_forgotten["primary_rounds"] == ["mastered", "mastered", "forgotten"]
    assert third_forgotten["status"] == STATUS_LEARNING
    assert third_forgotten["next_due_date"] == date(2026, 9, 11)

    # An extra same-day successful attempt cannot silently consume the failed
    # third round or advance to review; the card remains a carryover retry.
    additional = complete_new_card(
        third_forgotten,
        FIXED_STUDY_DATE,
        rating="mastered",
        additional_recall=True,
    )
    assert additional["status"] == STATUS_LEARNING
    assert additional["rounds_completed"] == 3
    assert additional["primary_rounds"] == ["mastered", "mastered", "forgotten"]
    assert additional["additional_rounds"] == 1
    assert additional["index"] == -1
    assert additional["next_due_date"] == date(2026, 9, 11)
    assert additional["same_day_additional"] is True


@pytest.mark.parametrize(
    ("study_date", "index", "expected"),
    [
        (date(2026, 1, 31), 0, date(2026, 2, 1)),
        (date(2026, 12, 31), 1, date(2027, 1, 3)),
        (date(2028, 2, 28), 0, date(2028, 2, 29)),
        (date(2028, 2, 29), 1, date(2028, 3, 3)),
        (date(2029, 2, 28), 0, date(2029, 3, 1)),
    ],
)
def test_next_due_date_cross_month_year_and_leap_day(study_date, index, expected):
    assert calculate_next_due_date(study_date, index) == expected


@pytest.mark.parametrize(
    ("due_date", "study_date", "expected"),
    [
        (date(2026, 9, 9), date(2026, 9, 10), True),
        (date(2026, 9, 10), date(2026, 9, 10), False),
        (date(2026, 9, 11), date(2026, 9, 10), False),
        ("2026-09-09", "2026-09-10", True),
    ],
)
def test_should_carryover_is_strictly_before_study_date(
    due_date, study_date, expected
):
    assert should_carryover(due_date, study_date) is expected


def test_same_day_additional_recall_does_not_advance_review_interval():
    first = transition_review("mastered", 0, FIXED_STUDY_DATE)
    second = transition_review(
        "mastered",
        first["index"],
        FIXED_STUDY_DATE,
        last_study_date=FIXED_STUDY_DATE,
        due_date=first["next_due_date"],
    )

    assert first["index"] == 1
    assert second["index"] == 1
    assert second["next_due_date"] == first["next_due_date"]
    assert second["same_day_additional"] is True


def test_transition_review_rejects_invalid_rating_and_index():
    with pytest.raises(ValueError):
        transition_review("unknown", 0, FIXED_STUDY_DATE)
    with pytest.raises(ValueError):
        transition_review("mastered", len(INTERVAL_DAYS), FIXED_STUDY_DATE)


def test_transition_state_fuzzy_schedules_next_day():
    from app.services.learning_scheduler import transition_state
    review_state = {
        "status": STATUS_REVIEW,
        "interval_index": 2,
        "interval_days": 7,
        "new_round": 3,
        "due_date": FIXED_STUDY_DATE,
    }
    result = transition_state(review_state, "fuzzy", review_date=FIXED_STUDY_DATE)
    assert result["status"] == STATUS_REVIEW
    assert result["interval_index"] == 2
    assert result["next_review_date"] == date(2026, 9, 11)


def test_transition_state_new_card_repetitions_do_not_bypass_rounds():
    from app.services.learning_scheduler import transition_state
    state_after_failures = {
        "status": STATUS_LEARNING,
        "interval_index": -1,
        "new_round": 0,
        "repetitions": 5,
        "due_date": FIXED_STUDY_DATE,
    }
    result = transition_state(state_after_failures, "mastered", review_date=FIXED_STUDY_DATE)
    assert result["status"] == STATUS_LEARNING
    assert result["new_round"] == 1
    assert result["next_review_date"] == FIXED_STUDY_DATE

