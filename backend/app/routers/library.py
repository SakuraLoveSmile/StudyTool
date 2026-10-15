"""Library endpoints inherit authentication/CSRF/tenant scope from AccessMiddleware."""
from fastapi import APIRouter, Header

from app.models.library import Answer, AskRequest, Chapter, Guide, GuideRequest
from app.services import library
from app.services.request_operations import run_idempotent

router = APIRouter(prefix="/api/library", tags=["library"])


@router.get("/chapters", response_model=list[Chapter])
def list_chapters(doc_id: str):
    return library.list_chapters(doc_id)


@router.get("/guides", response_model=list[Guide])
def list_guides(doc_id: str | None = None, chapter_id: str | None = None):
    return library.list_guides(doc_id, chapter_id)


@router.get("/guides/{guide_id}", response_model=Guide)
def get_guide(guide_id: str):
    return library.get_guide(guide_id)


@router.post("/guides", response_model=Guide)
async def generate_guide(req: GuideRequest, idempotency_key: str | None = Header(None, alias="Idempotency-Key")):
    result = await run_idempotent("library.guides", idempotency_key, req.model_dump(),
                                  lambda: library.generate_guide(req), Guide)
    # A replay must recheck current sources, rather than replaying an obsolete
    # source_stale=False from the durable idempotency record.
    return library.get_guide(result.id)


@router.post("/ask", response_model=Answer)
async def ask(req: AskRequest, idempotency_key: str | None = Header(None, alias="Idempotency-Key")):
    result = await run_idempotent("library.ask", idempotency_key, req.model_dump(),
                                  lambda: library.ask(req), Answer)
    return library.get_answer(result.id)


@router.get("/answers", response_model=list[Answer])
def list_answers(doc_id: str | None = None):
    return library.list_answers(doc_id)


@router.get("/answers/{answer_id}", response_model=Answer)
def get_answer(answer_id: str):
    return library.get_answer(answer_id)
