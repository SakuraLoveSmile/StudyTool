"""Useful task diagnostics must survive persistence without prompt/secret leakage."""
import json

import httpx
import pytest

from app.config import settings
from app.database import db_session
from app.services.http_client import HttpClientProvider
from app.services.llm.client import LLMClient
from app.services.llm.observability import LLMCallError, get_llm_diagnostic
from app.services.task_worker import TaskWorker
from app.services import library
from app.routers.materials import get_task_events,get_task_status


def _events(task_id):
    return get_task_events(task_id).model_dump()['events']


@pytest.mark.asyncio
async def test_task_timeline_persists_stage_and_safe_details(monkeypatch):
    task=TaskWorker.create_task('generate_library_guide','fixture')
    handler=TaskWorker.make_unit_event_handler(task,'guide-model','章节资料生成',stage='llm_generate',service='llm')
    base={'service':'llm','model':'fixture-model','endpoint':'https://user:password@api.example.test/v1/chat/completions?api_key=top-secret#hash',
          'timeout_seconds':60,'attempt':1,'max_attempts':3,'call_id':'call-123','elapsed_ms':60123,'stage_elapsed_ms':60001,
          'prompt':'教材正文机密','body':'响应内容机密','headers':{'Authorization':'Bearer top-secret'}}
    await handler('slot_wait_started',{**base,'stage':'slot_wait'})
    assert get_task_status(task).progress_detail.execUnits[0].state=='waiting_slot'
    await handler('request_start',{**base,'stage':'request_send'})
    await handler('network_stage',{**base,'stage':'await_headers','trace_event':'http11.receive_response_headers.started'})
    status=get_task_status(task)
    assert '响应头' in status.progress_detail.currentAction
    assert '响应头' in status.progress_detail.summary.waitingReason
    await handler('request_failed',{**base,'stage':'await_headers','failed_stage':'await_headers','exception_type':'ReadTimeout','will_retry':True})
    events=_events(task)
    failed=events[-1]
    assert '响应头' in failed['message'] and '60' in failed['message'] and 'fixture-model' in failed['message']
    assert '暂不能区分' in failed['message']
    assert failed['data']['timeout_seconds']==60 and failed['data']['call_id']=='call-123'
    assert failed['data']['endpoint']=='https://api.example.test/v1/chat/completions'
    raw=json.dumps(events,ensure_ascii=False)
    for secret in ('top-secret','教材正文机密','响应内容机密','password','Authorization'):
        assert secret not in raw
    # A retried error is recorded in the timeline, not left on a later success unit.
    assert get_task_status(task).progress_detail.execUnits[0].error is None


@pytest.mark.asyncio
@pytest.mark.parametrize('stage',['headers','body'])
async def test_real_llm_failure_reaches_task_and_final_error(monkeypatch,stage):
    monkeypatch.setattr(settings,'DEMO_MODE',False)
    monkeypatch.delenv('DEMO_MODE',raising=False)
    monkeypatch.setattr(settings,'LLM_API_KEY','sk-SECRET_CANARY_12345')
    monkeypatch.setattr(settings,'LLM_BASE_URL','https://api.example.test/v1')
    monkeypatch.setattr(settings,'LLM_MODEL','test-model')
    monkeypatch.setattr(settings,'LLM_TIMEOUT',60)
    task=TaskWorker.create_task('generate_library_guide','fixture')
    handler=TaskWorker.make_unit_event_handler(task,'guide-model','章节资料生成',stage='llm_generate',service='llm')
    class BrokenBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise httpx.ReadTimeout('PROMPT_SECRET BODY_SECRET sk-SECRET_CANARY_12345')
            yield b''
    async def transport(req):
        if stage=='headers': raise httpx.ReadTimeout('PROMPT_SECRET BODY_SECRET sk-SECRET_CANARY_12345')
        return httpx.Response(200,headers={'x-request-id':'upstream-req-1'},stream=BrokenBody())
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport),trust_env=False) as client:
        monkeypatch.setattr(HttpClientProvider,'get_client',classmethod(lambda cls:client))
        with pytest.raises(LLMCallError) as error:
            await LLMClient.chat_json('PROMPT_SECRET','BODY_SECRET',max_retries=0,on_event=handler)
    diagnostic=get_llm_diagnostic(error.value)
    assert diagnostic['failed_stage']==('await_headers' if stage=='headers' else 'read_body')
    wrapper=RuntimeError('untrusted provider error')
    wrapper.__cause__=error.value
    message=library.guide_task_error(wrapper)
    assert 'ReadTimeout' in message and 'test-model' in message and '60' in message
    assert ('响应头阶段' if stage=='headers' else '响应正文阶段') in message
    if stage=='body': assert '已收到响应头' in message
    TaskWorker.mark_task_failed(task,message)
    raw=json.dumps(_events(task),ensure_ascii=False)+get_task_status(task).error_msg
    for marker in ('PROMPT_SECRET','BODY_SECRET','sk-SECRET_CANARY_12345','untrusted provider error'):
        assert marker not in raw
    failures=[e for e in _events(task) if e['event_type']=='request_failed']
    assert len(failures)==1 and failures[0]['data']['will_retry'] is False
