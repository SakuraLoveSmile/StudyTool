"""V1 learning/content HTTP adapter.

The learning domain services intentionally stay independent from FastAPI.  This
router is therefore deliberately small: it translates request/body aliases,
forwards calls to the service layer, and presents stable error details to the
client.  Recall-card persistence was developed as an optional service, so the
content endpoints resolve it lazily and return an explicit 501 when an
installation does not provide it yet.
"""
from __future__ import annotations

import inspect
import json
import sqlite3
from collections.abc import Mapping
from typing import Any, Callable, Optional, Sequence

from fastapi import APIRouter, Body, HTTPException, Query

from app.database import db_session
from app.services import learning_service as learning_service
from app.services.learning_service import LearningService, LearningServiceError
from app.services import recall_generator as recall_generator
from app.services.recall_generator import RecallGenerationError, RecallGenerator, SourceChangedError

try:  # Recall-card service is optional in early/local installations.
    from app.services import recall_cards as recall_cards  # type: ignore
    from app.services.recall_cards import RecallCardsError  # type: ignore
except (ImportError, ModuleNotFoundError):  # pragma: no cover - depends on checkout stage
    recall_cards = None  # type: ignore[assignment]
    RecallCardsError = None  # type: ignore[assignment,misc]


router = APIRouter(prefix="/api/learning", tags=["learning"])


# ---------------------------------------------------------------------------
# Boundary helpers
# ---------------------------------------------------------------------------


_DOMAIN_ERROR_NAMES = {
    "LearningServiceError",
    "RecallGenerationError",
    "SourceChangedError",
    "RecallCardsError",
    "RecallCardError",
    "RecallCardServiceError",
    "RecallCardsServiceError",
}


def _mapping(value: Any) -> dict[str, Any]:
    """Return a permissive request mapping without requiring one Pydantic type."""

    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        try:
            return dict(value.model_dump(exclude_unset=True))
        except TypeError:
            return dict(value.model_dump())
    if hasattr(value, "dict"):
        try:
            return dict(value.dict(exclude_unset=True))
        except Exception:
            return dict(value.dict())
    if hasattr(value, "__dict__"):
        return {key: item for key, item in vars(value).items() if not key.startswith("_")}
    raise HTTPException(
        status_code=422,
        detail={"code": "INVALID_REQUEST", "message": "请求参数格式不正确"},
    )


def _first(data: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in data and data[name] is not None:
            return data[name]
    return default


def _list_value(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _dump(value: Any) -> Any:
    """Make Pydantic/SQLite results safe while retaining response shape."""

    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json")
        except TypeError:
            return value.model_dump()
    if isinstance(value, sqlite3.Row):
        return {key: value[key] for key in value.keys()}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, tuple):
        return list(value)
    return value


def _error_status(exc: BaseException) -> int:
    status = getattr(exc, "status_code", None)
    if status is not None:
        try:
            return int(status)
        except (TypeError, ValueError):
            pass
    name = type(exc).__name__
    if name == "SourceChangedError":
        return 409
    if name == "RecallGenerationError":
        return 422
    if name in {"RecallCardsError", "RecallCardError", "RecallCardServiceError", "RecallCardsServiceError"}:
        return 400
    return 400


def _error_detail(exc: BaseException) -> dict[str, str]:
    detail = getattr(exc, "detail", None)
    if isinstance(detail, Mapping) and detail.get("code") and detail.get("message"):
        return {"code": str(detail["code"]), "message": str(detail["message"])}
    code = getattr(exc, "code", None)
    if not code:
        code = {
            "LearningServiceError": "LEARNING_ERROR",
            "RecallGenerationError": "GENERATION_ERROR",
            "SourceChangedError": "SOURCE_CHANGED",
            "RecallCardsError": "CONTENT_ERROR",
            "RecallCardError": "CONTENT_ERROR",
            "RecallCardServiceError": "CONTENT_ERROR",
            "RecallCardsServiceError": "CONTENT_ERROR",
        }.get(type(exc).__name__, "LEARNING_ERROR")
    message = getattr(exc, "message", None) or str(exc) or "学习服务请求失败"
    return {"code": str(code), "message": str(message)}


def _is_domain_error(exc: BaseException) -> bool:
    if isinstance(exc, (LearningServiceError, RecallGenerationError, SourceChangedError)):
        return True
    # RecallCardsError has several useful subclasses (ConflictError,
    # SourceCitationError, ...); checking only the concrete class name would
    # leak those expected domain failures through Starlette's test/runtime
    # exception path.
    if RecallCardsError is not None and isinstance(exc, RecallCardsError):
        return True
    return type(exc).__name__ in _DOMAIN_ERROR_NAMES


def _raise_domain_http(exc: BaseException) -> None:
    raise HTTPException(status_code=_error_status(exc), detail=_error_detail(exc)) from exc


def _run(call: Callable[[], Any]) -> Any:
    """Invoke a service method and translate only expected domain failures."""

    try:
        return call()
    except HTTPException:
        raise
    except Exception as exc:
        if _is_domain_error(exc):
            _raise_domain_http(exc)
        raise


def _service_candidates(module: Any, owners: Sequence[str]) -> list[Any]:
    if module is None:
        return []
    # Prefer the concrete service class over its module-level compatibility
    # aliases.  The aliases intentionally expose ``*args, **kwargs`` and can
    # hide the stricter method signatures needed to drop unsupported UI fields
    # (for example ``attempt_id`` on LearningService.next_item).
    result: list[Any] = []
    for owner_name in owners:
        owner = getattr(module, owner_name, None)
        if owner is not None:
            result.append(owner)
    result.append(module)
    return result


def _find_callable(module: Any, names: Sequence[str], owners: Sequence[str] = ()) -> Optional[Callable[..., Any]]:
    for candidate in _service_candidates(module, owners):
        for name in names:
            method = getattr(candidate, name, None)
            if callable(method):
                return method
    return None


def _signature(fn: Callable[..., Any]) -> Optional[inspect.Signature]:
    try:
        return inspect.signature(fn)
    except (TypeError, ValueError):
        return None


def _invoke(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call both current service signatures and small test doubles safely."""

    signature = _signature(fn)
    if signature is None:
        return fn(*args, **kwargs)
    parameters = list(signature.parameters.values())
    accepts_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters)
    accepts_varargs = any(parameter.kind == inspect.Parameter.VAR_POSITIONAL for parameter in parameters)

    if accepts_kwargs or accepts_varargs:
        if not accepts_kwargs:
            kwargs = {key: value for key, value in kwargs.items() if key in signature.parameters}
        return fn(*args, **kwargs)

    # For strict test doubles such as f(chapter_id, units), turn canonical
    # keyword values into positional arguments when their names are present.
    positional = [
        parameter
        for parameter in parameters
        if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    remaining_args = list(args)
    consumed = len(remaining_args)
    for parameter in positional[consumed:]:
        if parameter.name in kwargs:
            remaining_args.append(kwargs.pop(parameter.name))
            consumed += 1
        elif parameter.default is inspect.Parameter.empty:
            break
        else:
            break
    filtered_kwargs = {
        key: value
        for key, value in kwargs.items()
        if key in signature.parameters
        and signature.parameters[key].kind
        in (inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    }
    return fn(*remaining_args, **filtered_kwargs)


def _invoke_payload(fn: Callable[..., Any], payload: Mapping[str, Any], *prefix: Any) -> Any:
    """Pass a body as ``data`` where supported, or as a positional request."""

    body = dict(payload)
    signature = _signature(fn)
    if signature is None:
        return fn(*prefix, data=body, **body)
    parameters = list(signature.parameters.values())
    accepts_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters)
    payload_names = ("data", "request", "body", "payload", "values", "input")
    target = next((name for name in payload_names if name in signature.parameters), None)
    if target:
        # The payload is already a mapping.  Do not also splat it into
        # ``**kwargs``: an id in the body (for example ``card_id``) would
        # collide with the path id supplied in ``prefix``.  The real services
        # merge/normalise the mapping themselves, and strict test doubles can
        # still receive it through their named ``data``/``request`` parameter.
        return _invoke(fn, *prefix, **{target: body})
    if accepts_kwargs:
        return _invoke(fn, *prefix, **body)

    # A strict f(request) / f(card_id, request) test double.
    positional = [
        parameter
        for parameter in parameters
        if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if len(positional) > len(prefix):
        return _invoke(fn, *prefix, body)
    return _invoke(fn, *prefix)


def _learning_method(names: Sequence[str]) -> Callable[..., Any]:
    method = _find_callable(learning_service, names, owners=("LearningService",))
    if method is None:
        raise HTTPException(
            status_code=501,
            detail={"code": "LEARNING_SERVICE_UNAVAILABLE", "message": "学习服务不可用"},
        )
    return method


def _learning_payload(names: Sequence[str], payload: Mapping[str, Any], *prefix: Any) -> Any:
    fn = _learning_method(names)
    return _invoke_payload(fn, payload, *prefix)


def _learning_kwargs(names: Sequence[str], *args: Any, **kwargs: Any) -> Any:
    fn = _learning_method(names)
    return _invoke(fn, *args, **kwargs)


def _content_method(names: Sequence[str]) -> Callable[..., Any]:
    method = _find_callable(
        recall_cards,
        names,
        owners=("RecallCardsService", "RecallCardService", "RecallCards", "RecallCardStore", "CardService"),
    )
    if method is None:
        raise HTTPException(
            status_code=501,
            detail={"code": "CONTENT_SERVICE_UNAVAILABLE", "message": "回忆卡内容服务尚未安装"},
        )
    return method


def _content_payload(names: Sequence[str], payload: Mapping[str, Any], *prefix: Any) -> Any:
    return _invoke_payload(_content_method(names), payload, *prefix)


def _content_kwargs(names: Sequence[str], *args: Any, **kwargs: Any) -> Any:
    return _invoke(_content_method(names), *args, **kwargs)


def _map_collection(value: Any, keys: Sequence[str], mapper: Callable[[Any], Any]) -> Any:
    value = _dump(value)
    if isinstance(value, list):
        return [mapper(item) for item in value]
    if isinstance(value, Mapping):
        result = dict(value)
        for key in keys:
            if isinstance(result.get(key), list):
                result[key] = [mapper(item) for item in result[key]]
                break
        return result
    return value


# ---------------------------------------------------------------------------
# Response compatibility shims
# ---------------------------------------------------------------------------


def _settings_response(value: Any) -> Any:
    value = _dump(value)
    if not isinstance(value, Mapping) or "selected_chapter_ids_json" not in value:
        return value
    result = dict(value)
    result.setdefault("new_cards_per_day", result.get("daily_new_limit"))
    result.setdefault("review_cards_per_day", result.get("daily_review_limit"))
    result.setdefault("reveal_after_seconds", 0)
    result.setdefault("auto_advance", False)
    result.setdefault("show_hints", True)
    return result


def _scope_response(value: Any, request: Optional[Mapping[str, Any]] = None) -> Any:
    value = _dump(value)
    if not isinstance(value, Mapping):
        return value
    result = dict(value)
    if "selected_chapter_ids" in result and "chapter_ids" not in result:
        result["chapter_ids"] = list(result.get("selected_chapter_ids") or [])
    elif "selected_chapter_ids_json" in result and "chapter_ids" not in result:
        try:
            result["chapter_ids"] = json.loads(result.get("selected_chapter_ids_json") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            result["chapter_ids"] = []
    if request and "include_unassigned" in request:
        result.setdefault("include_unassigned", bool(request["include_unassigned"]))
    else:
        result.setdefault("include_unassigned", True)
    return result


def _chapter_response(value: Any) -> Any:
    value = _dump(value)
    if not isinstance(value, Mapping):
        return value
    # Only enrich DB-shaped rows.  Already-shaped service/test responses are
    # returned untouched so integrations can retain their own envelope.
    if not any(key in value for key in ("cards_count", "ready_cards_count", "normalized_title", "metadata_json")):
        return value
    result = dict(value)
    result.setdefault("name", result.get("title") or "")
    result.setdefault("title", result.get("name") or "")
    result.setdefault("normalized_title", result.get("title_normalized") or result.get("name") or "")
    result.setdefault("card_count", int(result.get("cards_count") or 0))
    result.setdefault("due_count", int(result.get("ready_cards_count") or 0))
    result["enabled"] = bool(result.get("enabled", True))
    result.setdefault("status", "active")
    result.setdefault("version", int(result.get("version") or 1))
    metadata = result.get("metadata")
    if metadata is None and result.get("metadata_json"):
        try:
            result["metadata"] = json.loads(result["metadata_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            result["metadata"] = None
    return result


def _card_response(value: Any) -> Any:
    value = _dump(value)
    if not isinstance(value, Mapping):
        return value
    if not any(key in value for key in ("tags_json", "current_revision_id", "is_archived", "lock_version")):
        return value
    result = dict(value)
    result.setdefault("chapter_name", result.get("chapter_title") or result.get("chapter"))
    result.setdefault("prompt", result.get("question") or result.get("front_content") or "")
    result.setdefault("answer", result.get("back_content") or result.get("explanation") or "")
    tags = result.get("tags")
    if tags is None:
        try:
            tags = json.loads(result.get("tags_json") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            tags = []
        result["tags"] = tags if isinstance(tags, list) else []
    citations = result.get("source_citations")
    if citations is None:
        try:
            citations = json.loads(result.get("source_citations_json") or result.get("citations_json") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            citations = []
        result["source_citations"] = citations if isinstance(citations, list) else []
    result.setdefault("source_refs", result.get("source_citations") or [])
    result.setdefault("revision_id", result.get("current_revision_id"))
    result["is_confirmed"] = bool(result.get("is_confirmed", False))
    result["is_ai_generated"] = bool(result.get("is_ai_generated", False))
    result.setdefault("status", "new")
    result.setdefault("version", int(result.get("version") or 1))
    result.setdefault("review_count", 0)
    return result


def _generation_response(value: Any) -> Any:
    value = _dump(value)
    if not isinstance(value, Mapping):
        return value
    if not any(key in value for key in ("batch_id", "candidate_count", "error_message", "batch_fingerprint")):
        return value
    result = dict(value)
    chapter_id = result.get("chapter_id")
    chapter_ids = result.get("chapter_ids")
    if chapter_ids is None:
        result["chapter_ids"] = [chapter_id] if chapter_id else []
    result.setdefault("card_count", int(result.get("candidate_count") or 0))
    result.setdefault("created_count", int(result.get("accepted_count") or 0))
    result.setdefault("error_msg", result.get("error_message"))
    return result


def _invoke_generation_lookup(fn: Callable[..., Any], generation_id: str) -> Any:
    """Invoke a generator lookup regardless of its identifier parameter name."""

    signature = _signature(fn)
    if signature is None:
        return fn(generation_id)
    parameters = list(signature.parameters.values())
    if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        # Public aliases generally accept **kwargs; request the canonical name
        # while retaining a positional value for implementations using *args.
        return fn(generation_id, generation_id=generation_id, batch_id=generation_id, id=generation_id)
    positional = [
        parameter
        for parameter in parameters
        if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if positional:
        return fn(generation_id)
    for name in ("generation_id", "batch_id", "id"):
        if name in signature.parameters:
            return fn(**{name: generation_id})
    return fn()


def _today_response(value: Any) -> Any:
    value = _dump(value)
    if not isinstance(value, Mapping):
        return value
    if not any(key in value for key in ("study_date", "counts", "next_item", "day_version")):
        return value
    result = dict(value)
    result.setdefault("date", result.get("study_date"))
    counts = result.get("counts") if isinstance(result.get("counts"), Mapping) else {}
    result.setdefault("total", counts.get("total", len(result.get("units") or [])))
    result.setdefault("completed", counts.get("completed", 0))
    result.setdefault("remaining", counts.get("pending", max(int(result.get("total") or 0) - int(result.get("completed") or 0), 0)))
    result.setdefault("new_count", counts.get("new", 0))
    result.setdefault("review_count", counts.get("review", 0) + counts.get("carryover", 0))
    result.setdefault("attempts", result.get("units") or [])
    result.setdefault("current_attempt", result.get("next_item"))
    return result


# ---------------------------------------------------------------------------
# Settings, scope, and chapters
# ---------------------------------------------------------------------------


@router.get("/settings")
def get_learning_settings() -> Any:
    return _run(lambda: _settings_response(_learning_kwargs(("get_settings",))))


@router.put("/settings")
def update_learning_settings(body: Any = Body(default=None)) -> Any:
    payload = _mapping(body)
    return _run(lambda: _settings_response(_learning_payload(("update_settings",), payload)))


@router.put("/scope")
def update_learning_scope(body: Any = Body(default=None)) -> Any:
    payload = _mapping(body)
    # Keep both canonical and service-era names.  Existing LearningService uses
    # selected_chapter_ids; the V1 client uses chapter_ids.
    if "chapter_ids" in payload and "selected_chapter_ids" not in payload:
        payload["selected_chapter_ids"] = payload["chapter_ids"]
    if "knowledge_point_ids" in payload and "selected_knowledge_point_ids" not in payload:
        payload["selected_knowledge_point_ids"] = payload["knowledge_point_ids"]
    result = _run(lambda: _learning_payload(("save_scope", "update_scope"), payload))
    return _scope_response(result, payload)


@router.get("/chapters")
def list_learning_chapters() -> Any:
    # Chapters are content-management records, so prefer RecallCardsService.
    # LearningService remains a compatibility fallback for an older checkout
    # where recall_cards.py has not been installed yet.
    def call() -> Any:
        method = _find_callable(
            recall_cards,
            ("list_chapters", "get_chapters"),
            owners=("RecallCardsService", "RecallCardService"),
        )
        if method is not None:
            return _invoke(method)
        return _learning_kwargs(("list_chapters", "get_chapters"))

    result = _run(call)
    return _map_collection(result, ("items", "chapters"), _chapter_response)


@router.post("/chapters")
def create_learning_chapter(body: Any = Body(default=None)) -> Any:
    payload = _mapping(body)

    def call() -> Any:
        method = _find_callable(
            recall_cards,
            ("create_chapter",),
            owners=("RecallCardsService", "RecallCardService"),
        )
        if method is not None:
            return _invoke_payload(method, payload)
        return _learning_payload(("create_chapter",), payload)

    return _run(lambda: _chapter_response(call()))


@router.patch("/chapters/{chapter_id}")
def update_learning_chapter(chapter_id: str, body: Any = Body(default=None)) -> Any:
    payload = _mapping(body)

    def call() -> Any:
        method = _find_callable(
            recall_cards,
            ("update_chapter",),
            owners=("RecallCardsService", "RecallCardService"),
        )
        if method is not None:
            return _invoke_payload(method, payload, chapter_id)
        return _learning_payload(("update_chapter",), payload, chapter_id)

    return _run(lambda: _chapter_response(call()))


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def _generation_kwargs(payload: Mapping[str, Any]) -> dict[str, Any]:
    chapters = _list_value(_first(payload, "chapter_ids", "chapters"))
    chapter_id = _first(payload, "chapter_id", "chapterId")
    if chapter_id is None and chapters:
        chapter_id = chapters[0]
    units = _first(payload, "units", "knowledge_points", "knowledge_point_units")
    knowledge_point_ids = _first(payload, "knowledge_point_ids", "knowledgePointIds", "kp_ids")
    result: dict[str, Any] = {
        "chapter_id": chapter_id,
        "units": units,
        "knowledge_point_ids": knowledge_point_ids,
        "request_id": _first(payload, "request_id", "requestId", "idempotency_key"),
        "mode": _first(payload, "mode", "generation_mode", default="formal"),
        "source_doc_id": _first(payload, "source_doc_id", "sourceDocId"),
        "message": _first(payload, "message", "instructions"),
        # RecallGenerator accepts request= and extracts aliases itself.  Keeping
        # this also makes the adapter compatible with later generator versions.
        "request": dict(payload),
    }
    return {key: value for key, value in result.items() if value is not None}


@router.post("/generations")
def create_learning_generation(body: Any = Body(default=None)) -> Any:
    payload = _mapping(body)
    kwargs = _generation_kwargs(payload)

    def call() -> Any:
        method = _find_callable(recall_generator, ("create_generation_batch",), owners=("RecallGenerator",))
        if method is None:
            raise HTTPException(
                status_code=501,
                detail={"code": "GENERATION_SERVICE_UNAVAILABLE", "message": "记忆卡生成服务不可用"},
            )
        return _generation_response(_invoke(method, **kwargs))

    return _run(call)


def _get_generation_from_db(generation_id: str) -> Any:
    try:
        with db_session() as conn:
            row = conn.execute(
                "SELECT * FROM recall_generation_batches WHERE id = ?",
                (generation_id,),
            ).fetchone()
    except sqlite3.OperationalError as exc:
        raise HTTPException(
            status_code=501,
            detail={"code": "GENERATION_SERVICE_UNAVAILABLE", "message": "生成批次查询服务不可用"},
        ) from exc
    if not row:
        raise LearningServiceError("GENERATION_NOT_FOUND", "生成批次不存在", 404)

    metadata_raw = row["metadata_json"] if "metadata_json" in row.keys() else None
    try:
        metadata = json.loads(metadata_raw) if metadata_raw else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        metadata = {}
    formatter = getattr(RecallGenerator, "_batch_response", None)
    if callable(formatter):
        return _generation_response(formatter(row, metadata))
    result = dict(row)
    result["metadata"] = metadata
    return _generation_response(result)


@router.get("/generations")
def list_learning_generations(limit: int = Query(default=100, ge=1, le=500), offset: int = Query(default=0, ge=0)) -> Any:
    result = _run(lambda: _content_kwargs(("list_generations", "list_generation_batches"), limit=limit, offset=offset))
    return _map_collection(result, ("items", "generations", "batches"), _generation_response)


@router.get("/generations/{generation_id}")
def get_learning_generation(generation_id: str) -> Any:
    def call() -> Any:
        method = _find_callable(
            recall_generator,
            ("get_generation_batch", "get_batch", "get_generation", "get_generation_by_id"),
            owners=("RecallGenerator",),
        )
        if method is not None:
            result = _invoke_generation_lookup(method, generation_id)
            if result is None:
                raise LearningServiceError("GENERATION_NOT_FOUND", "生成批次不存在", 404)
            return _generation_response(result)
        return _get_generation_from_db(generation_id)

    return _run(call)


# ---------------------------------------------------------------------------
# Recall-card content
# ---------------------------------------------------------------------------


@router.post("/cards/{card_id}/publish")
def publish_learning_card(card_id: str, body: Any = Body(default=None)) -> Any:
    payload = _mapping(body)
    result = _run(lambda: _content_kwargs(("publish_card",), card_id=card_id, revision_id=payload.get("revision_id"), reset_progress=bool(payload.get("reset_progress", False)), expected_version=payload.get("expected_version") or payload.get("version")))
    return _card_response(result)


@router.post("/cards/{card_id}/lifecycle")
def set_learning_card_lifecycle(card_id: str, body: Any = Body(default=None)) -> Any:
    payload = _mapping(body)
    status = payload.get("status") or payload.get("lifecycle_status")
    result = _run(lambda: _content_kwargs(("set_card_lifecycle",), card_id=card_id, status=status, expected_version=payload.get("expected_version") or payload.get("version")))
    return _card_response(result)


@router.get("/cards")
def list_learning_cards(
    chapter_id: Optional[str] = Query(default=None),
    kp: Optional[str] = Query(default=None),
    knowledge_point_id: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
    include_candidates: bool = Query(default=False, description="包含草稿/AI 候选，用于人工审核"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
) -> Any:
    if status == "all":
        status = None
    kwargs = {
        "chapter_id": chapter_id,
        "knowledge_point_id": knowledge_point_id or kp,
        "kp": kp or knowledge_point_id,
        "status": status,
        "search": search,
        "include_candidates": include_candidates,
        "offset": offset,
        "limit": limit,
    }
    kwargs = {key: value for key, value in kwargs.items() if value is not None}
    result = _run(lambda: _content_kwargs(("list_cards", "list_recall_cards", "get_cards", "query_cards"), **kwargs))
    return _map_collection(result, ("items", "cards", "results"), _card_response)


@router.post("/cards")
def create_learning_card(body: Any = Body(default=None)) -> Any:
    payload = _mapping(body)
    result = _run(lambda: _content_payload(("create_card", "create_recall_card", "add_card"), payload))
    return _card_response(result)


@router.post("/cards/{card_id}/revisions")
def create_learning_card_revision(card_id: str, body: Any = Body(default=None)) -> Any:
    payload = _mapping(body)
    result = _run(lambda: _content_payload(("create_revision", "create_card_revision", "add_revision"), payload, card_id))
    return _card_response(result)


@router.post("/revisions/review")
def review_learning_revisions(body: Any = Body(default=None)) -> Any:
    payload = _mapping(body)
    result = _run(
        lambda: _content_payload(
            ("review_revisions", "review_revision", "review_card_revision", "apply_revision_review"),
            payload,
        )
    )
    return _card_response(result)


@router.patch("/cards/{card_id}")
def update_learning_card(card_id: str, body: Any = Body(default=None)) -> Any:
    payload = _mapping(body)
    result = _run(lambda: _content_payload(("update_card", "update_recall_card", "edit_card"), payload, card_id))
    return _card_response(result)


@router.get("/cards/{card_id}/history")
def get_learning_card_history(card_id: str) -> Any:
    result = _run(
        lambda: _content_kwargs(
            ("get_history", "get_card_history", "card_history", "history"),
            card_id=card_id,
        )
    )
    return _map_collection(result, ("items", "history", "revisions"), _card_response)


# ---------------------------------------------------------------------------
# Daily learning lifecycle
# ---------------------------------------------------------------------------


def _day_payload(body: Any, study_date: Optional[str] = None) -> dict[str, Any]:
    payload = _mapping(body)
    if study_date is not None:
        payload.setdefault("study_date", study_date)
    if "date" in payload and "study_date" not in payload:
        payload["study_date"] = payload["date"]
    if "day_version" not in payload and "state_version" in payload:
        payload["day_version"] = payload["state_version"]
    return payload


def _day_kwargs(payload: Mapping[str, Any], *, include: Sequence[str], live_day: bool = False) -> dict[str, Any]:
    aliases = {
        "study_date": ("study_date", "date"),
        "day_version": ("day_version", "expected_day_version"),
        "state_version": ("state_version",),
        "now": ("now",),
        "current_date": ("current_date",),
        "enforce_today": ("enforce_today",),
        "override": ("override", "override_enabled", "enabled"),
        "resume": ("resume",),
        "day_unit_id": ("day_unit_id", "unit_id"),
        "unit_id": ("unit_id", "day_unit_id"),
        "card_id": ("card_id",),
        "layer": ("layer",),
        "attempt_id": ("attempt_id",),
        "attempt_index": ("attempt_index",),
        "round": ("round",),
        "content_revision_no": ("content_revision_no", "card_revision_no"),
        "rating": ("rating", "value"),
        "request_id": ("request_id", "idempotency_key", "requestId"),
    }
    result: dict[str, Any] = {}
    for target in include:
        value = _first(payload, *aliases.get(target, (target,)))
        if value is not None:
            result[target] = value
    if live_day and "enforce_today" not in result:
        # Daily routes are live-day actions: a stale study_date must surface as
        # DAY_CHANGED instead of silently mutating a historical day.
        result["enforce_today"] = True
    return result


@router.get("/today")
def get_today_learning(study_date: Optional[str] = Query(default=None)) -> Any:
    kwargs: dict[str, Any] = {"enforce_today": True}
    if study_date is not None:
        kwargs["study_date"] = study_date
    result = _run(lambda: _learning_kwargs(("get_today",), **kwargs))
    return _today_response(result)


@router.post("/today/open")
def open_today_learning(body: Any = Body(default=None), study_date: Optional[str] = Query(default=None)) -> Any:
    payload = _day_payload(body, study_date)
    kwargs = _day_kwargs(
        payload,
        include=("study_date", "day_version", "state_version", "now", "current_date", "enforce_today", "override", "resume"),
        live_day=True,
    )
    result = _run(lambda: _learning_kwargs(("open_today",), **kwargs))
    return _today_response(result)


@router.post("/today/next")
def next_today_learning(body: Any = Body(default=None), study_date: Optional[str] = Query(default=None)) -> Any:
    payload = _day_payload(body, study_date)
    kwargs = _day_kwargs(
        payload,
        include=("study_date", "day_unit_id", "unit_id", "day_version", "state_version", "now", "current_date", "enforce_today"),
        live_day=True,
    )
    # Return a complete day envelope so clients do not lose queue state when
    # asking for the next item.  The service's next_item result is intentionally
    # kept inside the summary's next_item field.
    def call() -> Any:
        item = _learning_kwargs(("next_item", "next"), **kwargs)
        date_value = kwargs.get("study_date")
        summary = _learning_kwargs(("get_today",), study_date=date_value, now=kwargs.get("now"), current_date=kwargs.get("current_date"), enforce_today=kwargs.get("enforce_today"))
        if isinstance(summary, Mapping):
            result = dict(summary)
            result["next_item"] = item if isinstance(item, Mapping) and not item.get("done") else summary.get("next_item")
            return result
        return summary
    return _today_response(_run(call))


@router.post("/today/override")
def override_today_learning(body: Any = Body(default=None), study_date: Optional[str] = Query(default=None)) -> Any:
    payload = _day_payload(body, study_date)
    kwargs = _day_kwargs(
        payload,
        include=("study_date", "day_version", "state_version", "enabled", "override", "now", "current_date", "enforce_today", "card_id", "day_unit_id", "layer"),
        live_day=True,
    )
    # ``enabled`` is the V1 semantic; current LearningService accepts it while
    # newer services may call it override_enabled.
    if "override" in kwargs and "enabled" not in kwargs:
        kwargs["enabled"] = kwargs.pop("override")
    result = _run(lambda: _learning_kwargs(("override", "set_override"), **kwargs))
    return _today_response(result)


@router.post("/attempts/{attempt_id}/reveal")
def reveal_learning_attempt(
    attempt_id: str,
    body: Any = Body(default=None),
    study_date: Optional[str] = Query(default=None),
) -> Any:
    payload = _day_payload(body, study_date)
    kwargs = _day_kwargs(
        payload,
        include=(
            "study_date",
            "attempt_id",
            "day_unit_id",
            "unit_id",
            "card_id",
            "layer",
            "round",
            "attempt_index",
            "day_version",
            "state_version",
            "content_revision_no",
            "now",
            "current_date",
            "enforce_today",
        ),
        live_day=True,
    )
    kwargs["attempt_id"] = attempt_id
    result = _run(lambda: _learning_kwargs(("reveal", "reveal_attempt"), **kwargs))
    return _today_response(result)


@router.post("/attempts/{attempt_id}/rate")
def rate_learning_attempt(
    attempt_id: str,
    body: Any = Body(default=None),
    study_date: Optional[str] = Query(default=None),
) -> Any:
    payload = _day_payload(body, study_date)
    kwargs = _day_kwargs(
        payload,
        include=(
            "study_date",
            "attempt_id",
            "day_unit_id",
            "unit_id",
            "card_id",
            "layer",
            "round",
            "attempt_index",
            "rating",
            "request_id",
            "day_version",
            "state_version",
            "content_revision_no",
            "now",
            "current_date",
            "enforce_today",
        ),
        live_day=True,
    )
    kwargs["attempt_id"] = attempt_id
    result = _run(lambda: _learning_kwargs(("rate", "rate_attempt"), **kwargs))
    return _today_response(result)


@router.post("/today/advance")
def advance_today_learning(body: Any = Body(default=None), study_date: Optional[str] = Query(default=None)) -> Any:
    payload = _day_payload(body, study_date)
    kwargs = _day_kwargs(
        payload,
        include=("study_date", "day_version", "state_version", "now", "current_date", "enforce_today"),
        live_day=True,
    )
    result = _run(lambda: _learning_kwargs(("advance",), **kwargs))
    return _today_response(result)


@router.post("/today/pause")
def pause_today_learning(body: Any = Body(default=None), study_date: Optional[str] = Query(default=None)) -> Any:
    payload = _day_payload(body, study_date)
    kwargs = _day_kwargs(
        payload,
        include=("study_date", "day_version", "state_version", "now", "current_date", "enforce_today"),
        live_day=True,
    )
    result = _run(lambda: _learning_kwargs(("pause",), **kwargs))
    return _today_response(result)


__all__ = ["router"]
