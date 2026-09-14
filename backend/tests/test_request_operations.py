import asyncio
import json
from pydantic import BaseModel
import pytest

from app.config import settings
from app.database import db_session
from app.routers.cards import _question_answer_assistant
from app.routers.practice import generate_practice
from app.models.schemas import QuestionAnswerRequest, PracticeGenerateRequest
from app.services import request_operations
from app.services.request_operations import run_idempotent
from app.tenancy import tenant_scope


class Result(BaseModel):
    value: str


@pytest.mark.asyncio
async def test_same_key_runs_once_and_replays_result():
    calls = 0
    async def execute():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return Result(value="stable")
    first, second = await asyncio.gather(*[
        run_idempotent("test.same", "key", {"x": 1}, execute, Result),
        run_idempotent("test.same", "key", {"x": 1}, execute, Result),
    ])
    assert calls == 1
    assert first == second == Result(value="stable")


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_operation():
    calls = 0
    gate = asyncio.Event()
    async def execute():
        nonlocal calls
        calls += 1
        await gate.wait()
        return Result(value="done")
    waiter = asyncio.create_task(run_idempotent("test.cancel", "key", {}, execute, Result))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    gate.set()
    assert await run_idempotent("test.cancel", "key", {}, execute, Result) == Result(value="done")
    assert calls == 1


@pytest.mark.asyncio
async def test_payload_conflict_and_failed_retry():
    await run_idempotent("test.conflict", "key", {"x": 1}, lambda: _ok("first"), Result)
    with pytest.raises(Exception) as conflict:
        await run_idempotent("test.conflict", "key", {"x": 2}, lambda: _ok("never"), Result)
    assert getattr(conflict.value, "status_code", None) == 409
    calls = 0
    async def fail_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("expected")
        return Result(value="retry")
    with pytest.raises(RuntimeError):
        await run_idempotent("test.retry", "key", {}, fail_once, Result)
    assert await run_idempotent("test.retry", "key", {}, fail_once, Result) == Result(value="retry")


async def _ok(value):
    return Result(value=value)


@pytest.mark.asyncio
async def test_same_key_isolated_by_tenant():
    async def execute_a(): return Result(value="a")
    async def execute_b(): return Result(value="b")
    with tenant_scope("00000000-0000-0000-0000-000000000001"):
        a = await run_idempotent("test.tenant", "key", {}, execute_a, Result)
    with tenant_scope("00000000-0000-0000-0000-000000000002"):
        b = await run_idempotent("test.tenant", "key", {}, execute_b, Result)
    assert (a.value, b.value) == ("a", "b")


@pytest.mark.asyncio
async def test_restart_pending_operation_is_retryable():
    request_operations._tasks.clear()
    request_operations._ensure_table()
    with db_session() as conn:
        conn.execute("INSERT OR REPLACE INTO request_operations(id,route,idempotency_key,payload_hash,state) VALUES ('restart','test.restart','key',?,'pending')", (request_operations._payload_hash({}),))
    result = await run_idempotent("test.restart", "key", {}, lambda: _ok("recovered"), Result)
    assert result.value == "recovered"


@pytest.mark.asyncio
async def test_formal_empty_questions_fails_without_insert(monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    with db_session() as conn:
        conn.execute("INSERT INTO knowledge_points(id,name,is_deleted) VALUES ('kp-empty','空题考点',0)")
    async def empty(*args, **kwargs): return {"questions": []}
    monkeypatch.setattr("app.routers.practice.LLMClient.chat_json", empty)
    with pytest.raises(Exception) as failure:
        await generate_practice(PracticeGenerateRequest(knowledge_point_id="kp-empty"))
    assert getattr(failure.value, "status_code", None) == 502
    with db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM practice_sessions WHERE knowledge_point_id='kp-empty'").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_unknown_question_id_is_404():
    with pytest.raises(Exception) as failure:
        await _question_answer_assistant(QuestionAnswerRequest(question_id="missing"))
    assert getattr(failure.value, "status_code", None) == 404


@pytest.mark.asyncio
async def test_idempotent_practice_preserves_difficulty_and_grading_rubric(monkeypatch):
    from app.routers.practice import generate_practice, submit_and_evaluate_practice
    from app.models.schemas import PracticeGenerateRequest, PracticeSubmitRequest, PracticeAnswerSubmission
    from app.services.retrieval.hybrid import HybridSearchService
    from app.services.llm.client import LLMClient
    with db_session() as conn:
        conn.execute("INSERT INTO knowledge_points(id,name) VALUES('rubric-kp','测试考点')")
    async def search(*args, **kwargs):
        return []
    prompts = []
    async def model(system, prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            return {'questions': [{'stem': '比较概念', 'reference_answer': '说明区别',
                'evaluation_points': [{'point': '关键区别', 'score': 7, 'basis': '指定依据'}]}]}
        return {'keypoints_coverage': [], 'score_estimate': 7, 'max_score': 7}
    monkeypatch.setattr(HybridSearchService, 'search', search)
    monkeypatch.setattr(LLMClient, 'chat_json', model)
    session = await generate_practice(PracticeGenerateRequest(knowledge_point_id='rubric-kp', question_count=1,
        difficulty='comprehensive'), 'difficulty-key')
    assert '综合应用' in prompts[0]
    assert 'reference_answer' not in session.questions[0]
    await submit_and_evaluate_practice(session.id, PracticeSubmitRequest(answers=[PracticeAnswerSubmission(
        question_id=session.questions[0]['question_id'], answer_text='我的答案')]), 'rubric-key')
    assert '关键区别' in prompts[1] and '指定依据' in prompts[1] and '7' in prompts[1]
