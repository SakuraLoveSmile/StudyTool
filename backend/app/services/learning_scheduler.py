"""Pure scheduling helpers for the daily-learning recall flow.

The scheduler deliberately has no persistence or transport concerns.  Callers
pass the date on which a recall happened (or the date for which a queue is
being built) explicitly, and receive a new state value.  This keeps date
arithmetic deterministic and makes the state machine straightforward to test.

The public state representation is a plain ``dict`` so it can be stored by a
caller in any persistence layer.  :class:`LearningState` is provided as a
small typed convenience for callers that prefer attributes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Optional, Sequence, Union

# Keep this a list (rather than deriving it elsewhere): these are the product
# intervals and are part of the Phase A contract.
INTERVAL_DAYS = [1, 3, 7, 14, 30]
RATINGS = ("forgotten", "fuzzy", "mastered")
NEW_CARD_ROUNDS = 3

STATUS_NEW = "new"
STATUS_LEARNING = "learning"
STATUS_REVIEW = "review"

DateLike = Union[date, datetime, str]
StateLike = Union[Mapping[str, Any], "LearningState"]


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def _as_date(value: DateLike, *, name: str = "date") -> date:
    """Return a ``date`` without consulting the clock.

    ISO ``YYYY-MM-DD`` strings are accepted at boundaries because JSON-backed
    callers commonly deserialize dates as strings.  Datetimes are reduced to
    their calendar date; no timezone conversion is attempted.
    """

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:  # pragma: no cover - message is the useful bit
            raise ValueError(f"{name} must be an ISO date (YYYY-MM-DD)") from exc
    raise TypeError(f"{name} must be a date, datetime, or ISO date string")


def _date_or_none(value: Any, *, name: str) -> Optional[date]:
    if value is None:
        return None
    return _as_date(value, name=name)


def add_days(on_date: DateLike, days: int) -> date:
    """Add an interval to an explicit calendar date."""

    if not isinstance(days, int) or isinstance(days, bool):
        raise TypeError("days must be an integer")
    return _as_date(on_date, name="on_date") + timedelta(days=days)


def interval_days(interval_index: int) -> int:
    """Return the configured number of days for an interval index.

    Indexes are zero-based: index ``0`` is the one-day interval and index ``4``
    is the thirty-day interval.  The final interval is intentionally capped by
    the caller when a mastered card is already at the end of the ladder.
    """

    if not isinstance(interval_index, int) or isinstance(interval_index, bool):
        raise TypeError("interval_index must be an integer")
    if not 0 <= interval_index < len(INTERVAL_DAYS):
        raise ValueError(
            f"interval_index must be between 0 and {len(INTERVAL_DAYS) - 1}"
        )
    return INTERVAL_DAYS[interval_index]


def calculate_next_review_date(
    review_date: DateLike,
    interval: Optional[int] = None,
    *,
    interval_index: Optional[int] = None,
) -> date:
    """Calculate the next date from an explicit review date.

    Either a day count (``interval``) or an ``interval_index`` may be supplied.
    The function never uses ``date.today()``; this is important for replaying
    migrations and for deterministic tests around month/year boundaries.
    """

    if interval is None and interval_index is None:
        raise TypeError("provide interval or interval_index explicitly")
    if interval is not None and interval_index is not None:
        raise TypeError("provide interval or interval_index, not both")
    days = interval_days(interval_index) if interval_index is not None else interval
    if not isinstance(days, int) or isinstance(days, bool):
        raise TypeError("interval must be an integer")
    if days < 0:
        raise ValueError("interval must not be negative")
    return add_days(review_date, days)


def calculate_next_due_date(
    study_date: DateLike,
    interval_index: Optional[int] = None,
    *,
    index: Optional[int] = None,
    interval: Optional[int] = None,
    days: Optional[int] = None,
) -> date:
    """Return the due date for an interval from an explicit study date.

    The normal Phase A form is ``calculate_next_due_date(study_date, index)``
    where ``index`` selects one of :data:`INTERVAL_DAYS`.  ``interval`` and
    ``days`` are accepted as explicit day-count forms for migration callers.
    """

    selected_index = interval_index
    if index is not None:
        if selected_index is not None:
            raise TypeError("provide interval_index or index, not both")
        selected_index = index
    provided_counts = [value for value in (interval, days) if value is not None]
    if selected_index is not None and provided_counts:
        raise TypeError("provide an interval index or an explicit day count")
    if len(provided_counts) > 1:
        raise TypeError("provide interval or days, not both")
    if selected_index is None and not provided_counts:
        raise TypeError("an interval index or day count is required")
    if selected_index is not None:
        return calculate_next_review_date(study_date, interval_index=selected_index)
    return calculate_next_review_date(study_date, interval=provided_counts[0])


# Common names used by integrations and by earlier drafts of the plan.
next_review_date = calculate_next_review_date
get_next_review_date = calculate_next_review_date
calculate_next_recall_date = calculate_next_review_date


# ---------------------------------------------------------------------------
# State representation and migration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LearningState:
    """Typed view of a scheduler state.

    ``interval_index`` is ``-1`` while a card is new/learning.  A review card
    always has an index in ``0..4``.  ``new_round`` records successful new-card
    rounds and is capped at three when the card enters review.
    """

    status: str = STATUS_NEW
    interval_index: int = -1
    new_round: int = 0
    next_review_date: Optional[date] = None
    last_review_date: Optional[date] = None
    carryover: bool = False
    version: int = 1

    @property
    def phase(self) -> str:
        return self.status

    @property
    def interval_days(self) -> int:
        if self.interval_index < 0:
            return 0
        return interval_days(self.interval_index)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "phase": self.status,
            "interval_index": self.interval_index,
            "interval_days": self.interval_days,
            "new_round": self.new_round,
            "new_rounds_completed": self.new_round,
            "next_review_date": self.next_review_date,
            "due_date": self.next_review_date,
            "last_review_date": self.last_review_date,
            "last_recall_date": self.last_review_date,
            "carryover": self.carryover,
            "version": self.version,
        }

    @classmethod
    def from_mapping(cls, state: StateLike) -> "LearningState":
        normalized = _normalize_state(state)
        return cls(
            status=normalized["status"],
            interval_index=normalized["interval_index"],
            new_round=normalized["new_round"],
            next_review_date=normalized["next_review_date"],
            last_review_date=normalized["last_review_date"],
            carryover=normalized["carryover"],
            version=normalized["version"],
        )


def _first_present(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _normalize_rating(rating: str) -> str:
    if not isinstance(rating, str):
        raise TypeError("rating must be one of: forgotten, fuzzy, mastered")
    normalized = rating.strip().lower().replace("-", "_").replace(" ", "_")
    # A few harmless aliases make JSON/UI boundaries less error prone while
    # keeping the canonical values required by the plan.
    aliases = {
        "forget": "forgotten",
        "forgot": "forgotten",
        "hard": "fuzzy",
        "uncertain": "fuzzy",
        "easy": "mastered",
        "master": "mastered",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in RATINGS:
        raise ValueError(f"rating must be one of: {', '.join(RATINGS)}")
    return normalized


def _status_from_state(state: Mapping[str, Any]) -> str:
    raw = _first_present(state, "status", "phase", "state")
    if raw is None:
        if state.get("is_new") or state.get("new_card"):
            return STATUS_NEW
        return STATUS_NEW
    value = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "fresh": STATUS_NEW,
        "new_card": STATUS_NEW,
        "newcard": STATUS_NEW,
        "learning_new": STATUS_LEARNING,
        "in_learning": STATUS_LEARNING,
        "relearning": STATUS_LEARNING,
        "mature": STATUS_REVIEW,
        "scheduled": STATUS_REVIEW,
        "due": STATUS_REVIEW,
    }
    value = aliases.get(value, value)
    if value not in {STATUS_NEW, STATUS_LEARNING, STATUS_REVIEW}:
        raise ValueError("status must be new, learning, or review")
    return value


def _interval_index_from_state(state: Mapping[str, Any], status: str) -> int:
    raw_index = _first_present(state, "interval_index", "review_index", "index")
    if raw_index is not None:
        try:
            index = int(raw_index)
        except (TypeError, ValueError) as exc:
            raise ValueError("interval_index must be an integer") from exc
        if index < -1 or index >= len(INTERVAL_DAYS):
            raise ValueError(
                f"interval_index must be between -1 and {len(INTERVAL_DAYS) - 1}"
            )
        return index

    raw_days = _first_present(state, "interval_days", "interval")
    if raw_days is not None:
        try:
            days = int(raw_days)
        except (TypeError, ValueError) as exc:
            raise ValueError("interval_days must be an integer") from exc
        if days in INTERVAL_DAYS:
            return INTERVAL_DAYS.index(days)
        raise ValueError(f"interval_days must be one of {INTERVAL_DAYS}")
    return 0 if status == STATUS_REVIEW else -1


def _round_from_state(state: Mapping[str, Any]) -> int:
    raw = _first_present(
        state,
        "new_round",
        "new_rounds_completed",
        "round",
        "rounds_completed",
        "repetitions",
    )
    if raw is None:
        return 0
    try:
        rounds = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("new_round must be an integer") from exc
    if rounds < 0:
        raise ValueError("new_round must not be negative")
    return min(rounds, NEW_CARD_ROUNDS)


def _normalize_state(state: StateLike | None) -> dict[str, Any]:
    if state is None:
        source: Mapping[str, Any] = {}
    elif isinstance(state, LearningState):
        source = state.to_dict()
    elif isinstance(state, Mapping):
        source = state
    else:
        raise TypeError("state must be a mapping, LearningState, or None")

    status = _status_from_state(source)
    index = _interval_index_from_state(source, status)
    rounds = _round_from_state(source)
    next_date = _date_or_none(
        _first_present(
            source,
            "next_review_date",
            "next_recall_date",
            "due_date",
            "scheduled_for",
        ),
        name="next_review_date",
    )
    last_date = _date_or_none(
        _first_present(source, "last_review_date", "last_recall_date", "reviewed_on"),
        name="last_review_date",
    )
    carryover_value = _first_present(source, "carryover", "is_carryover")
    version_value = _first_present(source, "version", "state_version")
    try:
        version = int(version_value) if version_value is not None else 1
    except (TypeError, ValueError) as exc:
        raise ValueError("version must be an integer") from exc

    # A migrated state should not claim to be a new card with a review index,
    # nor a review card with an impossible negative index.
    if status != STATUS_REVIEW:
        index = -1
    elif index < 0:
        index = 0
    if status == STATUS_REVIEW:
        rounds = NEW_CARD_ROUNDS

    normalized = dict(source)
    normalized.update(
        {
            "status": status,
            "phase": status,
            "interval_index": index,
            "interval_days": INTERVAL_DAYS[index] if index >= 0 else 0,
            "new_round": rounds,
            "new_rounds_completed": rounds,
            "next_review_date": next_date,
            "due_date": next_date,
            "last_review_date": last_date,
            "last_recall_date": last_date,
            "carryover": bool(carryover_value) if carryover_value is not None else False,
            "version": max(version, 1),
        }
    )
    return normalized


def migrate_state(
    state: StateLike | None = None,
    *,
    on_date: Optional[DateLike] = None,
    as_of_date: Optional[DateLike] = None,
    migration_date: Optional[DateLike] = None,
) -> dict[str, Any]:
    """Normalize a legacy state and optionally carry overdue work forward.

    ``on_date``/``as_of_date``/``migration_date`` are equivalent names for the
    explicit date at which migration is evaluated.  At most one may be given.
    If omitted, migration only normalizes fields and never consults the clock.
    """

    supplied = [value for value in (on_date, as_of_date, migration_date) if value is not None]
    if len(supplied) > 1:
        raise TypeError("provide only one of on_date, as_of_date, migration_date")
    evaluated_on = _as_date(supplied[0], name="on_date") if supplied else None
    normalized = _normalize_state(state)

    if evaluated_on is not None:
        due = normalized["next_review_date"]
        if due is None:
            # A card with no legacy due date is immediately actionable on the
            # migration date, but we do not invent a date when no date was
            # explicitly supplied.
            normalized["next_review_date"] = evaluated_on
            normalized["due_date"] = evaluated_on
        elif due < evaluated_on:
            normalized["next_review_date"] = evaluated_on
            normalized["due_date"] = evaluated_on
            normalized["carryover"] = True
        normalized["migration_date"] = evaluated_on
    return normalized


migrate_card_state = migrate_state
migrate_learning_state = migrate_state


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------


def _result_state(
    state: dict[str, Any],
    *,
    status: str,
    interval_index: int,
    new_round: int,
    next_date: Optional[date],
    review_date: date,
    rating: str,
    carryover: bool,
    same_day_additional: bool,
) -> dict[str, Any]:
    result = dict(state)
    result.update(
        {
            "status": status,
            "phase": status,
            "interval_index": interval_index,
            "interval_days": INTERVAL_DAYS[interval_index] if interval_index >= 0 else 0,
            "new_round": min(new_round, NEW_CARD_ROUNDS),
            "new_rounds_completed": min(new_round, NEW_CARD_ROUNDS),
            "next_review_date": next_date,
            "due_date": next_date,
            "last_review_date": review_date,
            "last_recall_date": review_date,
            "last_rating": rating,
            "rating": rating,
            "carryover": carryover,
            "same_day_additional": same_day_additional,
            "completed": status == STATUS_REVIEW,
            "version": max(int(result.get("version", 1)), 1),
        }
    )
    return result


def transition_state(
    state: StateLike | None,
    rating: str,
    *,
    review_date: DateLike,
) -> dict[str, Any]:
    """Apply one recall rating and return a new, migrated state.

    New cards require three ``mastered`` rounds.  While those rounds are being
    completed, the next date remains the same calendar day so a UI can present
    additional same-day rounds.  The third successful round moves the card to
    the one-day review interval.

    For review cards, ``mastered`` advances one interval, ``fuzzy`` repeats
    the current interval, and ``forgotten`` starts a one-day relearning step.
    An additional review on the same day never advances an interval.  A
    forgotten same-day answer is still allowed to reset the card, since that is
    a correction rather than interval advancement.
    """

    current_date = _as_date(review_date, name="review_date")
    normalized_rating = _normalize_rating(rating)
    current = migrate_state(state, on_date=current_date)
    status = current["status"]
    current_index = current["interval_index"]
    current_round = current["new_round"]
    due_before = current.get("next_review_date")
    previous_review = current.get("last_review_date")
    same_day = previous_review == current_date
    carried = bool(due_before is not None and due_before < current_date)

    if status in {STATUS_NEW, STATUS_LEARNING}:
        if normalized_rating == "forgotten":
            # A failed new-card attempt starts the three-round sequence over;
            # retrying tomorrow avoids an unbounded same-day loop.
            return _result_state(
                current,
                status=STATUS_LEARNING,
                interval_index=-1,
                new_round=0,
                next_date=current_date + timedelta(days=1),
                review_date=current_date,
                rating=normalized_rating,
                carryover=carried,
                same_day_additional=same_day,
            )

        if normalized_rating == "fuzzy":
            # Fuzzy is retained as progress-neutral and carried to the next
            # day.  The already-completed successful rounds remain intact.
            return _result_state(
                current,
                status=STATUS_LEARNING,
                interval_index=-1,
                new_round=current_round,
                next_date=current_date + timedelta(days=1),
                review_date=current_date,
                rating=normalized_rating,
                carryover=carried,
                same_day_additional=same_day,
            )

        # mastered
        completed_round = min(current_round + 1, NEW_CARD_ROUNDS)
        if completed_round >= NEW_CARD_ROUNDS:
            return _result_state(
                current,
                status=STATUS_REVIEW,
                interval_index=0,
                new_round=NEW_CARD_ROUNDS,
                next_date=current_date + timedelta(days=INTERVAL_DAYS[0]),
                review_date=current_date,
                rating=normalized_rating,
                carryover=carried,
                same_day_additional=same_day,
            )
        # Additional new-card rounds intentionally remain due today.  This is
        # not an interval transition and therefore works on the same date.
        return _result_state(
            current,
            status=STATUS_LEARNING,
            interval_index=-1,
            new_round=completed_round,
            next_date=current_date,
            review_date=current_date,
            rating=normalized_rating,
            carryover=carried,
            same_day_additional=same_day,
        )

    # Review card transitions.
    if current_index < 0:
        current_index = 0

    if same_day and normalized_rating in {"mastered", "fuzzy"}:
        # Do not let repeated taps/extra recall within a day skip intervals.
        return _result_state(
            current,
            status=STATUS_REVIEW,
            interval_index=current_index,
            new_round=NEW_CARD_ROUNDS,
            next_date=due_before,
            review_date=current_date,
            rating=normalized_rating,
            carryover=carried,
            same_day_additional=True,
        )

    if normalized_rating == "forgotten":
        # Relearning starts at the one-day step.  Keeping status=learning and
        # interval_index=0 records which review interval is being relearned;
        # it also avoids losing the distinction from a never-seen new card.
        return _result_state(
            current,
            status=STATUS_LEARNING,
            interval_index=0,
            new_round=0,
            next_date=current_date + timedelta(days=INTERVAL_DAYS[0]),
            review_date=current_date,
            rating=normalized_rating,
            carryover=carried,
            same_day_additional=same_day,
        )

    if normalized_rating == "fuzzy":
        next_index = current_index
    else:  # mastered
        next_index = min(current_index + 1, len(INTERVAL_DAYS) - 1)

    return _result_state(
        current,
        status=STATUS_REVIEW,
        interval_index=next_index,
        new_round=NEW_CARD_ROUNDS,
        next_date=current_date + timedelta(days=INTERVAL_DAYS[next_index]),
        review_date=current_date,
        rating=normalized_rating,
        carryover=carried,
        same_day_additional=False,
    )


# Several descriptive aliases keep the core operation discoverable without
# making callers depend on one historical spelling.
apply_rating = transition_state
update_card_state = transition_state
schedule_recall = transition_state
advance_state = transition_state
transition_card_state = transition_state


def transition(
    state: StateLike | None,
    rating: str,
    review_date: DateLike,
) -> dict[str, Any]:
    """Positional-date convenience wrapper around :func:`transition_state`."""

    return transition_state(state, rating, review_date=review_date)


# ---------------------------------------------------------------------------
# Phase A named APIs
# ---------------------------------------------------------------------------


def _review_transition_result(
    *,
    rating: str,
    old_index: int,
    new_index: int,
    study_date: date,
    next_due: date,
    same_day_additional: bool,
    carried_over: bool = False,
) -> dict[str, Any]:
    """Build a deliberately redundant JSON-friendly transition result.

    The scheduler is used at a boundary between UI and persistence code.  The
    canonical keys are ``index`` and ``next_due_date``; equivalent keys are
    included so callers using the older ``interval_index``/``due_date`` names
    can migrate without another state conversion.
    """

    return {
        "rating": rating,
        "study_date": study_date,
        "old_index": old_index,
        "previous_index": old_index,
        "index": new_index,
        "interval_index": new_index,
        "interval_days": INTERVAL_DAYS[new_index],
        "next_due_date": next_due,
        "next_review_date": next_due,
        "due_date": next_due,
        "same_day_additional": same_day_additional,
        "carried_over": carried_over,
        "carryover": carried_over,
        "status": STATUS_REVIEW,
        "completed": True,
    }


def transition_review(
    rating: str,
    index: int,
    study_date: DateLike,
    *,
    last_study_date: Optional[DateLike] = None,
    last_review_date: Optional[DateLike] = None,
    additional_recall: bool = False,
    same_day_additional: Optional[bool] = None,
    due_date: Optional[DateLike] = None,
) -> dict[str, Any]:
    """Transition an existing review card through one recall.

    ``index`` is zero-based into :data:`INTERVAL_DAYS`.  On a different day,
    ``mastered`` advances one step (capped at the final step), ``fuzzy``
    repeats the current step, and ``forgotten`` returns to the one-day step.
    A same-day additional recall is progress-neutral: it returns the current
    index rather than advancing it.  Pass ``last_study_date`` (or
    ``last_review_date``) when the caller has prior state, or set
    ``additional_recall=True`` for a stateless UI event.

    Dates are explicit and no database, network, or wall-clock calls occur.
    """

    review_date = _as_date(study_date, name="study_date")
    normalized_rating = _normalize_rating(rating)
    if not isinstance(index, int) or isinstance(index, bool):
        raise TypeError("index must be an integer")
    if not 0 <= index < len(INTERVAL_DAYS):
        raise ValueError(f"index must be between 0 and {len(INTERVAL_DAYS) - 1}")
    if last_study_date is not None and last_review_date is not None:
        raise TypeError("provide last_study_date or last_review_date, not both")
    previous_date_value = (
        last_study_date if last_study_date is not None else last_review_date
    )
    previous_date = (
        _as_date(previous_date_value, name="last_study_date")
        if previous_date_value is not None
        else None
    )
    if same_day_additional is not None:
        if additional_recall and not same_day_additional:
            raise TypeError("additional_recall conflicts with same_day_additional")
        additional_recall = bool(same_day_additional)
    is_same_day = additional_recall or previous_date == review_date

    # A prior due date is retained for same-day calls when available.  For the
    # three-argument pure API there is no prior due date, so derive it from the
    # unchanged interval and the explicit study date.
    previous_due = (
        _as_date(due_date, name="due_date") if due_date is not None else None
    )
    if is_same_day:
        next_index = index
        next_due = previous_due or (review_date + timedelta(days=INTERVAL_DAYS[index]))
    elif normalized_rating == "forgotten":
        next_index = 0
        next_due = review_date + timedelta(days=INTERVAL_DAYS[0])
    elif normalized_rating == "fuzzy":
        # Uncertain recall is a next-day retry, while retaining the current
        # interval index.  It must not wait the full current interval: a fuzzy
        # answer is deliberately still fragile.
        next_index = index
        next_due = review_date + timedelta(days=1)
    else:  # mastered
        next_index = min(index + 1, len(INTERVAL_DAYS) - 1)
        next_due = review_date + timedelta(days=INTERVAL_DAYS[next_index])

    return _review_transition_result(
        rating=normalized_rating,
        old_index=index,
        new_index=next_index,
        study_date=review_date,
        next_due=next_due,
        same_day_additional=is_same_day,
    )


def _parse_new_card_round(
    rounds_completed: Any,
    *,
    round_number: Optional[int],
    current_round: Optional[int],
) -> int:
    provided = [
        value
        for value in (round_number, current_round)
        if value is not None
    ]
    if len(provided) > 1:
        raise TypeError("provide only one of round_number and current_round")
    raw = provided[0] if provided else rounds_completed
    if isinstance(raw, Mapping):
        raw = _first_present(
            raw,
            "rounds_completed",
            "new_round",
            "new_rounds_completed",
            "round",
        )
        raw = 0 if raw is None else raw
    try:
        parsed = int(raw)
    except (TypeError, ValueError) as exc:
        raise TypeError("rounds_completed must be an integer") from exc
    if parsed < 0:
        raise ValueError("rounds_completed must not be negative")
    return min(parsed, NEW_CARD_ROUNDS)


def _new_card_ratings_from_state(value: Any) -> tuple[dict[str, Any], list[Optional[str]]]:
    """Extract primary-round ratings while accepting legacy round counters."""

    if isinstance(value, (list, tuple)):
        if len(value) > NEW_CARD_ROUNDS:
            raise ValueError(
                f"primary_rounds cannot contain more than {NEW_CARD_ROUNDS} entries"
            )
        ratings: list[Optional[str]] = []
        for raw_rating in value:
            if raw_rating is None or raw_rating == "":
                ratings.append(None)
            else:
                ratings.append(_normalize_rating(str(raw_rating)))
        return {}, ratings + ([None] * (NEW_CARD_ROUNDS - len(ratings)))

    if isinstance(value, Mapping):
        source = dict(value)
        raw_ratings = _first_present(
            source,
            "primary_rounds",
            "primary_ratings",
            "primary_results",
        )
        if raw_ratings is None:
            count = _parse_new_card_round(
                source,
                round_number=None,
                current_round=None,
            )
            ratings: list[Optional[str]] = ["mastered"] * count
        else:
            if not isinstance(raw_ratings, (list, tuple)):
                raise TypeError("primary_rounds must be a list or tuple")
            ratings = []
            for raw_rating in raw_ratings:
                if raw_rating is None or raw_rating == "":
                    ratings.append(None)
                else:
                    ratings.append(_normalize_rating(str(raw_rating)))
            if len(ratings) > NEW_CARD_ROUNDS:
                raise ValueError(
                    f"primary_rounds cannot contain more than {NEW_CARD_ROUNDS} entries"
                )
            ratings.extend([None] * (NEW_CARD_ROUNDS - len(ratings)))
        return source, ratings

    count = _parse_new_card_round(
        value,
        round_number=None,
        current_round=None,
    )
    # Preserve the three primary slots so the next call can select the first
    # missing slot.  A plain legacy count means those completed slots were
    # mastered; unfilled slots remain explicit ``None`` values.
    return {}, (["mastered"] * count) + ([None] * (NEW_CARD_ROUNDS - count))


def _new_card_target_round(
    ratings: list[Optional[str]],
    *,
    explicit_round: Optional[int],
    additional: bool,
) -> Optional[int]:
    if additional:
        return None
    if explicit_round is not None:
        target = explicit_round
        # Accept both the plan's human-facing 1..3 and a convenient 0-based
        # spelling where zero means the first primary round.
        if target == 0:
            target = 1
        if target < 1 or target > NEW_CARD_ROUNDS:
            raise ValueError(f"primary round must be between 1 and {NEW_CARD_ROUNDS}")
        return target
    try:
        first_missing = ratings.index(None)
    except ValueError:
        # A failed third primary can be retried on the following day without
        # requiring callers to know the internal slot number.
        return NEW_CARD_ROUNDS if len(ratings) >= NEW_CARD_ROUNDS else None
    return first_missing + 1


def complete_new_card(
    rounds_completed: Any = 0,
    study_date: Optional[DateLike] = None,
    *,
    round_number: Optional[int] = None,
    current_round: Optional[int] = None,
    primary_round: Optional[int] = None,
    rating: str = "mastered",
    additional_recall: bool = False,
    same_day_additional: Optional[bool] = None,
    is_additional: Optional[bool] = None,
    is_primary: Optional[bool] = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Apply one primary or additional new-card recall.

    A new card has exactly three *primary* slots.  A slot is considered
    attempted when it receives any canonical rating; ``forgotten`` and
    ``fuzzy`` in the first or second slot do not invalidate a later mastered
    third primary.  Completion requires all three primary slots to exist and
    the third primary rating to be ``mastered``.  Thus an additional recall
    never substitutes for a missing/failed primary slot.  A failed third slot
    (or a missing primary slot after the day ends) remains learning/carryover
    and is due the following day.

    ``rounds_completed`` may be an integer, a prior result mapping, or a
    legacy mapping with only ``new_round``/``rounds_completed``.  ``round_number``
    and ``primary_round`` are one-based slot selectors; if omitted, the first
    missing slot is selected, or slot three is retried after a failed third
    primary.  All dates are explicit.
    """

    # Permit ``complete_new_card(study_date, rounds_completed)`` as a natural
    # positional spelling, and a few JSON-boundary date aliases.
    if isinstance(rounds_completed, (date, datetime, str)) and isinstance(
        study_date, int
    ):
        rounds_completed, study_date = study_date, rounds_completed
    if study_date is None:
        study_date = kwargs.pop("date", kwargs.pop("on_date", kwargs.pop("recall_date", None)))
    if study_date is None:
        raise TypeError("study_date is required")
    current_date = _as_date(study_date, name="study_date")

    # Accept aliases used by callers that model an event rather than a UI
    # button.  Unknown kwargs are rejected instead of silently changing state.
    if "additional" in kwargs:
        if is_additional is not None and bool(is_additional) != bool(kwargs["additional"]):
            raise TypeError("conflicting additional flags")
        is_additional = bool(kwargs.pop("additional"))
    if "is_extra" in kwargs:
        if is_additional is not None and bool(is_additional) != bool(kwargs["is_extra"]):
            raise TypeError("conflicting additional flags")
        is_additional = bool(kwargs.pop("is_extra"))
    if "round" in kwargs:
        if primary_round is not None or round_number is not None:
            raise TypeError("provide only one primary round selector")
        primary_round = kwargs.pop("round")
    if "attempt_number" in kwargs:
        if primary_round is not None or round_number is not None:
            raise TypeError("provide only one primary round selector")
        primary_round = kwargs.pop("attempt_number")
    if kwargs:
        unknown = ", ".join(sorted(kwargs))
        raise TypeError(f"unexpected keyword argument(s): {unknown}")

    explicit_rounds = [value for value in (round_number, primary_round) if value is not None]
    if len(explicit_rounds) > 1:
        raise TypeError("provide only one of round_number and primary_round")
    explicit_round = explicit_rounds[0] if explicit_rounds else None
    if explicit_round is None and current_round is not None:
        # ``current_round`` means completed-count in the legacy API.
        try:
            explicit_round = int(current_round) + 1
        except (TypeError, ValueError) as exc:
            raise TypeError("current_round must be an integer") from exc

    if is_additional is None:
        is_additional = bool(additional_recall)
    elif additional_recall and not bool(is_additional):
        raise TypeError("additional_recall conflicts with is_additional")
    is_additional = bool(is_additional)
    if is_primary is not None:
        if bool(is_primary) == is_additional:
            raise TypeError("is_primary conflicts with additional flags")
        is_additional = not bool(is_primary)
    if same_day_additional is not None:
        if additional_recall and not same_day_additional:
            raise TypeError("additional_recall conflicts with same_day_additional")
        is_additional = bool(same_day_additional) or is_additional

    source, ratings = _new_card_ratings_from_state(rounds_completed)
    normalized_rating = _normalize_rating(rating)
    before_ratings = list(ratings)
    target_round = _new_card_target_round(
        ratings,
        explicit_round=explicit_round,
        additional=is_additional,
    )

    if is_additional:
        # Additional recalls are deliberately side-channel events.  They never
        # fill a primary slot, even when a third primary has failed.
        additional_count = int(source.get("additional_rounds", 0) or 0) + 1
    else:
        if target_round is None:  # defensive; non-additional always selects one
            raise AssertionError("a primary recall must select a slot")
        while len(ratings) < target_round:
            ratings.append(None)
        ratings[target_round - 1] = normalized_rating
        additional_count = int(source.get("additional_rounds", 0) or 0)

    attempted = sum(item is not None for item in ratings)
    all_primary_present = len(ratings) == NEW_CARD_ROUNDS and all(
        item is not None for item in ratings
    )
    third_rating = ratings[NEW_CARD_ROUNDS - 1] if len(ratings) >= NEW_CARD_ROUNDS else None
    completed = all_primary_present and third_rating == "mastered"

    if completed:
        next_due = current_date + timedelta(days=INTERVAL_DAYS[0])
    elif all_primary_present and third_rating in {"forgotten", "fuzzy"}:
        # A failed third primary is a carryover retry.  One or more additional
        # recalls on the same day must not change this date or make it review.
        next_due = current_date + timedelta(days=1)
    elif not all_primary_present:
        # Any incomplete new-card sequence carries over to the next day.  This
        # applies even when slot one or two was mastered: it prevents a card
        # from disappearing from the next-day queue before all primary rounds
        # have been attempted.
        next_due = current_date + timedelta(days=1)
    elif is_additional and source.get("next_due_date") is not None:
        prior_due = _date_or_none(source.get("next_due_date"), name="next_due_date")
        next_due = prior_due if prior_due and prior_due > current_date else current_date
    else:
        # Slots one and two remain available for same-day primary recall.
        next_due = current_date

    prior_due = _date_or_none(
        _first_present(source, "next_due_date", "next_review_date", "due_date"),
        name="next_due_date",
    )
    was_overdue = prior_due is not None and prior_due < current_date
    carryover = bool(source.get("carryover", False)) or was_overdue
    if not completed and all_primary_present and third_rating != "mastered":
        carryover = True
    if not completed and not all_primary_present:
        carryover = True

    result = dict(source)
    result.update(
        {
            "rating": normalized_rating,
            "last_rating": normalized_rating,
            "study_date": current_date,
            "rounds_before": sum(item is not None for item in before_ratings),
            # Legacy names remain counts of attempted primary slots; an
            # additional event intentionally leaves this count unchanged.
            "rounds_completed": attempted,
            "new_round": attempted,
            "new_rounds_completed": attempted,
            "primary_rounds": ratings,
            "primary_ratings": ratings,
            "primary_rounds_attempted": attempted,
            "primary_rounds_complete": all_primary_present,
            "primary_missing_rounds": [
                position + 1 for position, item in enumerate(ratings) if item is None
            ],
            "primary_round": target_round,
            "round_number": target_round,
            "third_primary_rating": third_rating,
            "additional_rounds": additional_count,
            "additional_rating": normalized_rating if is_additional else source.get("additional_rating"),
            "is_additional": is_additional,
            "same_day_additional": bool(is_additional),
            "completed": completed,
            "status": STATUS_REVIEW if completed else STATUS_LEARNING,
            "index": 0 if completed else -1,
            "interval_index": 0 if completed else -1,
            "interval_days": INTERVAL_DAYS[0] if completed else 0,
            "next_due_date": next_due,
            "next_review_date": next_due,
            "due_date": next_due,
            "carryover": carryover,
        }
    )
    return result


def should_carryover(
    due_date: Any,
    study_date: Optional[DateLike] = None,
) -> bool:
    """Whether a card due before an explicit study date must carry over.

    The normal form is ``should_carryover(due_date, study_date)``.  A state
    mapping may be supplied as the first argument; its due date is read from
    ``next_due_date``, ``next_review_date``, or ``due_date``.  Due today is not
    considered carryover; only a strictly earlier date is overdue.
    """

    if isinstance(due_date, Mapping):
        state = due_date
        if study_date is None:
            raise TypeError("study_date is required")
        due_value = _first_present(
            state,
            "next_due_date",
            "next_review_date",
            "due_date",
            "next_recall_date",
        )
        if due_value is None:
            return False
        due_date = due_value
    if due_date is None:
        return False
    if study_date is None:
        raise TypeError("study_date is required")
    return _as_date(due_date, name="due_date") < _as_date(study_date, name="study_date")


# ---------------------------------------------------------------------------
# Carryover and daily queue helpers
# ---------------------------------------------------------------------------


def carryover_state(
    state: StateLike | None,
    *,
    on_date: DateLike,
) -> dict[str, Any]:
    """Move an overdue state to an explicit date without changing progress.

    Carryover is idempotent: applying it repeatedly for the same date does not
    alter the state after the first call.  A state due today is not marked as
    overdue/carryover, while any earlier due date is made actionable today.
    """

    evaluated_on = _as_date(on_date, name="on_date")
    result = _normalize_state(state)
    due = result.get("next_review_date")
    if due is None:
        result["next_review_date"] = evaluated_on
        result["due_date"] = evaluated_on
    elif due < evaluated_on:
        result["next_review_date"] = evaluated_on
        result["due_date"] = evaluated_on
        result["carryover"] = True
    result["migration_date"] = evaluated_on
    return result


carry_over = carryover_state
apply_carryover = carryover_state


def is_due(state: StateLike | None, *, on_date: DateLike) -> bool:
    """Whether a state belongs in the queue for ``on_date`` (including carryover)."""

    evaluated_on = _as_date(on_date, name="on_date")
    due = _normalize_state(state).get("next_review_date")
    return due is None or due <= evaluated_on


def due_for_recall(
    states: Sequence[StateLike],
    *,
    on_date: DateLike,
) -> list[dict[str, Any]]:
    """Return migrated due states in input order, without mutating inputs."""

    evaluated_on = _as_date(on_date, name="on_date")
    return [
        carryover_state(state, on_date=evaluated_on)
        for state in states
        if is_due(state, on_date=evaluated_on)
    ]


build_daily_recall = due_for_recall
get_due_cards = due_for_recall


__all__ = [
    "INTERVAL_DAYS",
    "RATINGS",
    "NEW_CARD_ROUNDS",
    "STATUS_NEW",
    "STATUS_LEARNING",
    "STATUS_REVIEW",
    "LearningState",
    "add_days",
    "interval_days",
    "calculate_next_review_date",
    "next_review_date",
    "get_next_review_date",
    "calculate_next_recall_date",
    "calculate_next_due_date",
    "migrate_state",
    "migrate_card_state",
    "migrate_learning_state",
    "transition_state",
    "transition_review",
    "complete_new_card",
    "should_carryover",
    "transition_card_state",
    "apply_rating",
    "update_card_state",
    "schedule_recall",
    "advance_state",
    "transition",
    "carryover_state",
    "carry_over",
    "apply_carryover",
    "is_due",
    "due_for_recall",
    "build_daily_recall",
    "get_due_cards",
]
