import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  classifyConnectionTestTransportFailure,
  normalizeConnectionTestResult,
} from '../connection-test';

describe('normalizeConnectionTestResult', () => {
  it('passes a successful probe through without logging a failure', () => {
    const outcome = normalizeConnectionTestResult(
      'llm',
      200,
      { ok: true, service: 'llm', call_id: 'call-abc12345', message: 'LLM 服务测试成功' },
      12.5
    );

    assert.equal(outcome.event, null);
    assert.equal(outcome.result.ok, true);
    assert.equal(outcome.result.call_id, 'call-abc12345');
    assert.deepEqual(outcome.fields, {});
  });

  it('logs a backend-reported LLM failure with its correlation id', () => {
    const outcome = normalizeConnectionTestResult(
      'llm',
      200,
      {
        ok: false,
        service: 'llm',
        status_code: 401,
        error_type: 'auth_failed',
        call_id: 'call-deadbeef',
        message: 'LLM API Key 无效或未授权 (401)',
      },
      15
    );

    assert.equal(outcome.event, 'connection_test.failed');
    assert.equal(outcome.result.error_type, 'auth_failed');
    assert.equal(outcome.fields.call_id, 'call-deadbeef');
    assert.equal(outcome.fields.status_code, 401);
    assert.equal(outcome.fields.error_type, 'auth_failed');
    assert.equal(outcome.fields.service, 'llm');
  });

  it('normalizes a FastAPI HTTPException body so the page shows a reason', () => {
    const outcome = normalizeConnectionTestResult(
      'embedding',
      400,
      { detail: 'Embedding API Key 未配置' },
      3
    );

    assert.equal(outcome.event, 'connection_test.failed');
    assert.equal(outcome.result.ok, false);
    assert.equal(outcome.result.message, 'Embedding API Key 未配置');
    assert.equal(outcome.result.error_type, 'backend_error');
    assert.equal(outcome.fields.status_code, 400);
  });

  it('treats a non-JSON backend reply as an invalid response', () => {
    const outcome = normalizeConnectionTestResult('llm', 502, '<html>bad gateway</html>', 4);

    assert.equal(outcome.event, 'connection_test.invalid_response');
    assert.equal(outcome.result.ok, false);
    assert.equal(outcome.result.error_type, 'invalid_backend_response');
    assert.equal(outcome.fields.status_code, 502);
    assert.ok(String(outcome.result.message).includes('502'));
  });

  it('flags a failure payload returned with a 5xx status even without ok=false', () => {
    const outcome = normalizeConnectionTestResult('llm', 500, { service: 'llm' }, 9);

    assert.equal(outcome.event, 'connection_test.failed');
    assert.equal(outcome.fields.error_type, 'backend_error');
    assert.equal(outcome.fields.status_code, 500);
  });
});

describe('classifyConnectionTestTransportFailure', () => {
  it('distinguishes an unreachable local backend from an LLM probe failure', () => {
    const outcome = classifyConnectionTestTransportFailure(
      'llm',
      { code: 'ECONNREFUSED', name: 'Error' },
      7
    );

    assert.equal(outcome.event, 'connection_test.unreachable');
    assert.equal(outcome.result.ok, false);
    assert.equal(outcome.result.error_type, 'backend_unreachable');
    assert.equal(outcome.fields.reason, 'ECONNREFUSED');
    assert.equal(outcome.fields.error_type, 'backend_unreachable');
  });

  it('falls back to the error name when no code is present', () => {
    const outcome = classifyConnectionTestTransportFailure('llm', { name: 'AbortError' }, 1);

    assert.equal(outcome.fields.reason, 'AbortError');
  });

  it('survives a missing error object', () => {
    const outcome = classifyConnectionTestTransportFailure('llm', undefined, 1);

    assert.equal(outcome.event, 'connection_test.unreachable');
    assert.equal(outcome.fields.reason, undefined);
  });
});
