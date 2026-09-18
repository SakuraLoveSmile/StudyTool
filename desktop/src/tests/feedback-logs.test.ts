/**
 * Tests for the restricted feedback-log collection (worker channel + collector).
 *
 * Two layers are covered:
 *  - collectFeedbackLogs() directly: source mapping, session/window filtering,
 *    rotation, re-whitelisting/re-scrubbing, per-file caps and the failure contract;
 *  - LogWorkerService.feedback(): the third worker channel — BUSY instead of a
 *    queue, timeout that really kills the thread, crash/cancel normalization into
 *    the LOG_FEEDBACK_* contract, isolation from query/export, and recovery.
 */

import test from 'node:test';
import assert from 'node:assert';
import fs from 'fs';
import os from 'os';
import path from 'path';
import {
  collectFeedbackLogs,
  FeedbackCollectionError,
  FEEDBACK_FILE_MAX_BYTES,
  type FeedbackJobPayload,
  type FeedbackLogsSuccess,
} from '../feedback-logs';
import { LogWorkerService } from '../log-workers';
import { LOG_FEEDBACK_TIMEOUT_MS } from '../log-worker-protocol';

const NOW = Date.now();
const SESS = 'sess-fb';

function tempDir(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'rag-feedback-'));
}

/** Accepts plain objects (serialized) and raw strings (written verbatim). */
function writeLog(dir: string, source: string, lines: unknown[]): void {
  fs.writeFileSync(
    path.join(dir, `${source}.jsonl`),
    lines.map((l) => (typeof l === 'string' ? l : JSON.stringify(l))).join('\n') + '\n'
  );
}

function entry(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    timestamp: new Date(NOW - 60_000).toISOString(),
    level: 'INFO',
    source: 'desktop',
    event: 'test.event',
    message: 'hello',
    session_id: SESS,
    app_version: '1.0.5',
    ...overrides,
  };
}

function payload(dir: string, overrides: Partial<FeedbackJobPayload> = {}): FeedbackJobPayload {
  return { dir, sessionId: SESS, appVersion: '1.0.5', now: NOW, ...overrides };
}

/** Splits a collected file into its metadata line and its record lines. */
function parseFile(text: string): { meta: Record<string, unknown>; records: Record<string, unknown>[] } {
  const lines = text.split('\n').filter((l) => l.length > 0);
  assert.ok(lines.length >= 1, 'a collected file has at least the metadata line');
  const meta = JSON.parse(lines[0]) as Record<string, unknown>;
  const records = lines.slice(1).map((l) => JSON.parse(l) as Record<string, unknown>);
  return { meta, records };
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// --- Collector semantics --------------------------------------------------------

test('collects all three sources across rotated files, filtered by session and window', () => {
  const dir = tempDir();
  writeLog(dir, 'desktop', [
    entry({ timestamp: new Date(NOW - 1 * 60_000).toISOString(), event: 'd.active' }),
    entry({ timestamp: new Date(NOW - 40 * 60_000).toISOString(), event: 'd.outside.window' }),
    entry({ event: 'd.other.session', session_id: 'sess-other' }),
    entry({ event: 'd.no.session', session_id: undefined }),
  ]);
  // Rotated generations (.1 newer than .2) still contribute in-window records.
  fs.writeFileSync(
    path.join(dir, 'desktop.jsonl.1'),
    `${JSON.stringify(entry({ timestamp: new Date(NOW - 10 * 60_000).toISOString(), event: 'd.rot1' }))}\n`
  );
  fs.writeFileSync(
    path.join(dir, 'desktop.jsonl.2'),
    `${JSON.stringify(entry({ timestamp: new Date(NOW - 20 * 60_000).toISOString(), event: 'd.rot2' }))}\n`
  );
  writeLog(dir, 'backend', [
    entry({ source: 'backend', timestamp: new Date(NOW - 5 * 60_000).toISOString(), event: 'b.one' }),
  ]);
  writeLog(dir, 'frontend', [
    entry({ source: 'frontend', timestamp: new Date(NOW - 3 * 60_000).toISOString(), event: 'f.one' }),
  ]);

  const result = collectFeedbackLogs(payload(dir));
  assert.strictEqual(result.ok, true);
  assert.deepStrictEqual(
    result.files.map((f) => f.filename).sort(),
    ['studytool-backend.jsonl', 'studytool-desktop.jsonl', 'studytool-frontend.jsonl']
  );
  assert.deepStrictEqual(result.unavailable, []);
  assert.strictEqual(result.truncated, false);
  assert.strictEqual(result.skippedLines, 0);

  const desktop = result.files.find((f) => f.filename === 'studytool-desktop.jsonl')!;
  const { meta, records } = parseFile(desktop.text);
  assert.strictEqual(meta.kind, 'feedback-log-collection');
  assert.strictEqual(meta.app_version, '1.0.5');
  assert.strictEqual(meta.platform, process.platform);
  assert.strictEqual(meta.arch, process.arch);
  assert.strictEqual(meta.source, 'desktop');
  assert.strictEqual(meta.from, new Date(NOW - 30 * 60_000).toISOString(), 'window is fixed at 30 min');
  assert.strictEqual(meta.to, new Date(NOW).toISOString());
  assert.strictEqual(meta.records, 3);
  assert.strictEqual(meta.truncated, false);
  assert.deepStrictEqual(meta.unavailable, []);
  assert.strictEqual(meta.skipped_lines, 0);
  assert.ok(!desktop.text.includes(dir), 'the log directory must never appear in the payload');

  assert.deepStrictEqual(
    records.map((r) => r.event),
    ['d.rot2', 'd.rot1', 'd.active'],
    'records ascend by timestamp_ms across rotated files'
  );
  const stamps = records.map((r) => r.timestamp_ms as number);
  assert.deepStrictEqual([...stamps].sort((a, b) => a - b), stamps);
  assert.ok(records.every((r) => r.session_id === SESS));
  assert.ok(records.every((r) => r.source === 'desktop'));

  const backend = result.files.find((f) => f.filename === 'studytool-backend.jsonl')!;
  assert.strictEqual(parseFile(backend.text).records[0].event, 'b.one');
  const frontend = result.files.find((f) => f.filename === 'studytool-frontend.jsonl')!;
  assert.strictEqual(parseFile(frontend.text).records[0].event, 'f.one');
});

test('partial availability: missing sources are listed, the rest still ships', () => {
  const dir = tempDir();
  writeLog(dir, 'desktop', [entry({ event: 'only.desktop' })]);

  const result = collectFeedbackLogs(payload(dir));
  assert.strictEqual(result.ok, true);
  assert.deepStrictEqual(
    result.files.map((f) => f.filename),
    ['studytool-desktop.jsonl']
  );
  assert.deepStrictEqual(result.unavailable, ['backend', 'frontend']);
  const { meta } = parseFile(result.files[0].text);
  assert.deepStrictEqual(meta.unavailable, ['backend', 'frontend']);
});

test('no valid records plus unreadable sources fails with LOG_FEEDBACK_UNAVAILABLE', () => {
  const dir = tempDir(); // empty: every source is missing
  try {
    collectFeedbackLogs(payload(dir));
    assert.fail('must throw');
  } catch (err: any) {
    assert.ok(err instanceof FeedbackCollectionError);
    assert.strictEqual(err.code, 'LOG_FEEDBACK_UNAVAILABLE');
    assert.ok(!String(err.message).includes(dir), 'the failure message carries no raw path');
  }
});

test('all sources readable but zero matches is an empty success, not a failure', () => {
  const dir = tempDir();
  writeLog(dir, 'desktop', [entry({ session_id: 'sess-other' })]);
  writeLog(dir, 'backend', [entry({ source: 'backend', session_id: 'sess-other' })]);
  writeLog(dir, 'frontend', [entry({ source: 'frontend', session_id: 'sess-other' })]);

  const result = collectFeedbackLogs(payload(dir));
  assert.strictEqual(result.ok, true);
  assert.deepStrictEqual(result.files, [], 'a readable-but-empty window produces no bare-meta files');
  assert.deepStrictEqual(result.unavailable, []);
});

test('malformed, plain-text and oversized lines are skipped and never uploaded raw', () => {
  const dir = tempDir();
  const hugeLine = JSON.stringify(entry({ event: 'huge', message: 'y'.repeat(300 * 1024) }));
  writeLog(dir, 'desktop', [
    JSON.stringify(entry({ event: 'ok.before', timestamp: new Date(NOW - 2 * 60_000).toISOString() })),
    '{"truncated": ',
    'plain console line containing Bearer plaintextsecret999',
    hugeLine,
    JSON.stringify(entry({ event: 'ok.中文', message: '中文消息内容', timestamp: new Date(NOW - 60_000).toISOString() })),
  ]);

  const result = collectFeedbackLogs(payload(dir));
  assert.strictEqual(result.ok, true);
  assert.strictEqual(result.files.length, 1);
  assert.ok(result.skippedLines >= 1, 'the >256 KiB line is counted');
  const { meta, records } = parseFile(result.files[0].text);
  assert.deepStrictEqual(
    records.map((r) => r.event).sort(),
    ['ok.before', 'ok.中文'],
    'only well-formed session records survive'
  );
  assert.ok((meta.skipped_lines as number) >= 1);
  assert.ok(!result.files[0].text.includes('plaintextsecret999'), 'a non-JSON line never reaches the payload');
  // Nothing is ever cut mid-character: the payload round-trips through UTF-8
  // unchanged and every single line is complete, parseable JSON.
  const text = result.files[0].text;
  assert.strictEqual(Buffer.from(text, 'utf8').toString('utf8'), text);
});

test('second-pass whitelist+scrub strips secrets, non-schema fields and record_id', () => {
  const dir = tempDir();
  writeLog(dir, 'desktop', [
    entry({
      event: 'leak.probe',
      message:
        'key sk-1234567890abcdef Bearer abcdef1234567890 ' +
        'https://api.example.com/v1/items?access_token=zzz999&x=1 ' +
        'stored at /Users/alice/Documents/机密报告.pdf',
      api_key: 'sk-shouldneverleak',
      prompt: '业务提示词原文不应上传',
      record_id: 'desktop#0#1',
      detail: { message: 'Bearer nestedtoken12345', password: 'p-secret', file: '账单.xlsx' },
      stack: 'Error\n    at run (/Users/bob/app/dist/index.js:10:5)',
    }),
  ]);
  // A legacy-format record: the reader copies `msg` into `message` WITHOUT
  // scrubbing it — only the second pass here can catch the secret inside.
  writeLog(dir, 'backend', [
    JSON.stringify({
      // Legacy key names, ISO timestamp kept so the epoch conversion stays UTC.
      asctime: new Date(NOW - 5 * 60_000).toISOString(),
      levelname: 'ERROR',
      msg: 'provider failed Bearer legacymessage789',
      session_id: SESS,
      source: 'backend',
    }),
  ]);

  const result = collectFeedbackLogs(payload(dir));
  assert.strictEqual(result.files.length, 2);
  const all = result.files.map((f) => f.text).join('');

  for (const leaked of [
    'sk-1234567890abcdef',
    'abcdef1234567890',
    'zzz999',
    '/Users/alice',
    '机密报告',
    '业务提示词原文不应上传',
    'sk-shouldneverleak',
    'nestedtoken12345',
    'p-secret',
    'legacymessage789',
    '/Users/bob',
    '账单',
  ]) {
    assert.ok(!all.includes(leaked), `must not contain ${leaked}`);
  }
  assert.ok(!all.includes('"record_id"'), 'viewer-only record ids never leave the process');
  assert.ok(!all.includes('"api_key"'), 'non-whitelisted keys are dropped entirely');
  assert.ok(all.includes('***'), 'secrets are masked, not removed silently');

  const backend = result.files.find((f) => f.filename === 'studytool-backend.jsonl')!;
  const { records } = parseFile(backend.text);
  assert.strictEqual(records.length, 1, 'the legacy record is normalized, not dropped');
  assert.match(records[0].message as string, /Bearer \*\*\*/, 'the unscrubbed legacy msg is caught on pass two');
  assert.strictEqual(records[0].level, 'ERROR', 'levelname normalized to level');
});

test('the 256 KiB file cap drops the oldest whole lines and flags truncation', () => {
  const dir = tempDir();
  const base = NOW - 25 * 60_000;
  const many = Array.from({ length: 500 }, (_, i) =>
    entry({
      timestamp: new Date(base + i * 1000).toISOString(),
      event: `evt.${String(i).padStart(3, '0')}`,
      message: 'x'.repeat(500),
    })
  );
  writeLog(dir, 'desktop', many);

  const result = collectFeedbackLogs(payload(dir));
  assert.strictEqual(result.files.length, 1);
  const text = result.files[0].text;
  assert.ok(Buffer.byteLength(text, 'utf8') <= FEEDBACK_FILE_MAX_BYTES, 'the cap counts metadata too');
  assert.strictEqual(result.truncated, true);

  const { meta, records } = parseFile(text);
  assert.strictEqual(meta.truncated, true);
  assert.strictEqual(meta.records, records.length);
  assert.ok(records.length > 0 && records.length < 500);
  // Oldest dropped, newest kept, ascending order, every line still complete JSON.
  assert.strictEqual(records[records.length - 1].event, 'evt.499');
  const firstIdx = Number(String(records[0].event).slice(4));
  assert.ok(firstIdx > 0, `oldest records were dropped (first kept: ${records[0].event})`);
  const stamps = records.map((r) => r.timestamp_ms as number);
  assert.deepStrictEqual([...stamps].sort((a, b) => a - b), stamps);
});

test('multi-byte lines are trimmed as whole records — never cut mid-character', () => {
  const dir = tempDir();
  const base = NOW - 25 * 60_000;
  const many = Array.from({ length: 220 }, (_, i) =>
    entry({
      timestamp: new Date(base + i * 1000).toISOString(),
      event: `cn.${String(i).padStart(3, '0')}`,
      message: `中文多字节内容${'字'.repeat(450)}${i}`,
    })
  );
  writeLog(dir, 'desktop', many);

  const result = collectFeedbackLogs(payload(dir));
  const text = result.files[0].text;
  assert.ok(Buffer.byteLength(text, 'utf8') <= FEEDBACK_FILE_MAX_BYTES);
  assert.strictEqual(Buffer.from(text, 'utf8').toString('utf8'), text, 'payload is valid UTF-8 end to end');
  const { records } = parseFile(text); // every line parses — no partial line exists
  assert.strictEqual(records[records.length - 1].event, 'cn.219');
  assert.strictEqual(result.truncated, true);
});

test('more than 500 matching records in a source is reported as truncation', () => {
  const dir = tempDir();
  const base = NOW - 25 * 60_000;
  const many = Array.from({ length: 600 }, (_, i) =>
    entry({ timestamp: new Date(base + i * 100).toISOString(), event: `bulk.${i}` })
  );
  writeLog(dir, 'desktop', many);

  const result = collectFeedbackLogs(payload(dir));
  assert.strictEqual(result.truncated, true);
  const { meta, records } = parseFile(result.files[0].text);
  assert.strictEqual(records.length, 500, 'the per-source page caps at 500');
  assert.strictEqual(meta.truncated, true);
  assert.strictEqual(records[records.length - 1].event, 'bulk.599', 'the newest records win');
});

// --- Worker channel semantics -----------------------------------------------------

const FAKE_WORKER = `
const { parentPort } = require('worker_threads');
const fs = require('fs');
if (process.env.FAKE_WORKER_CRASH === '1') { process.exit(1); }
const hb = process.env.FAKE_WORKER_HEARTBEAT || '';
if (hb) {
  try { fs.writeFileSync(hb, 'start\\n'); } catch {}
  setInterval(() => { try { fs.appendFileSync(hb, '.'); } catch {} }, 10);
}
parentPort.on('message', (req) => {
  const p = req.payload || {};
  const reply = (v) => { try { parentPort.postMessage(v); } catch {} };
  if (req.kind === 'feedback') {
    if (p.hang) return; // never respond — the timeout must kill this thread
    const delay = Number(p.delayMs || 0);
    setTimeout(() => {
      if (p.staleFirst) reply({ status: 'result', requestId: 'bogus-request-id', value: { stale: true } });
      if (p.errorCode) {
        reply({ status: 'error', requestId: req.requestId, code: p.errorCode, message: 'fake worker failure' });
        return;
      }
      reply({
        status: 'result',
        requestId: req.requestId,
        value: { ok: true, files: [], truncated: false, unavailable: [], skippedLines: 0 },
      });
      if (p.doubleReply) {
        reply({ status: 'result', requestId: req.requestId, value: { late: true } });
      }
    }, delay);
    return;
  }
  if (req.kind === 'query') {
    reply({
      status: 'result',
      requestId: req.requestId,
      value: { ok: true, entries: [], total: 0, page: 1, pageSize: 200, hasMore: false,
               scannedBytes: 0, scanTruncated: false, unavailable: [], skippedLines: 0 },
    });
    return;
  }
  if (req.kind === 'export') {
    reply({ status: 'result', requestId: req.requestId, value: { ok: true, entries: 0 } });
  }
});
`;

function fakeWorkerPath(dir: string): string {
  const file = path.join(dir, 'fake-log-worker.js');
  fs.writeFileSync(file, FAKE_WORKER);
  return file;
}

test('the feedback timeout constant is the contracted 2500 ms', () => {
  assert.strictEqual(LOG_FEEDBACK_TIMEOUT_MS, 2500);
});

test('a real worker collects the contract payload end to end', async () => {
  const dir = tempDir();
  writeLog(dir, 'desktop', [entry({ event: 'e2e.desktop' })]);
  writeLog(dir, 'backend', [entry({ source: 'backend', event: 'e2e.backend' })]);

  const service = new LogWorkerService();
  try {
    const outcome = await service.feedback('f-e2e', payload(dir, { now: Date.now() }));
    assert.ok(outcome.ok, 'feedback job succeeded');
    if (!outcome.ok) return;
    const value = outcome.value;
    assert.strictEqual(value.ok, true, 'the resolved value IS the ok:true contract variant');
    assert.deepStrictEqual(
      value.files.map((f) => f.filename).sort(),
      ['studytool-backend.jsonl', 'studytool-desktop.jsonl']
    );
    assert.deepStrictEqual(value.unavailable, ['frontend']);
    const { records } = parseFile(value.files.find((f) => f.filename === 'studytool-desktop.jsonl')!.text);
    assert.strictEqual(records[0].event, 'e2e.desktop');
  } finally {
    service.shutdown();
  }
});

test('unreadable sources through the worker surface LOG_FEEDBACK_UNAVAILABLE', async () => {
  const dir = tempDir(); // empty
  const service = new LogWorkerService();
  try {
    const outcome = await service.feedback('f-unavailable', payload(dir));
    assert.ok(outcome.ok === false);
    if (!outcome.ok) {
      assert.strictEqual(outcome.error.code, 'LOG_FEEDBACK_UNAVAILABLE');
      assert.strictEqual(outcome.error.retryable, false);
      assert.ok(!outcome.error.message.includes(dir));
    }
  } finally {
    service.shutdown();
  }
});

test('a malformed job payload surfaces LOG_FEEDBACK_FAILED', async () => {
  const service = new LogWorkerService();
  try {
    const outcome = await service.feedback('f-bad', {
      dir: 42,
      sessionId: 'x',
      appVersion: '1',
      now: 1,
    } as unknown as FeedbackJobPayload);
    assert.ok(outcome.ok === false);
    if (!outcome.ok) assert.strictEqual(outcome.error.code, 'LOG_FEEDBACK_FAILED');
  } finally {
    service.shutdown();
  }
});

test('a second feedback request is answered BUSY while one is in flight, then recovers', async () => {
  const dir = tempDir();
  const service = new LogWorkerService({ workerPath: fakeWorkerPath(dir), feedbackTimeoutMs: 10_000 });
  try {
    const first = service.feedback('f-1', { ...payload(dir), delayMs: 300 } as FeedbackJobPayload);
    const second = service.feedback('f-2', payload(dir));
    const [r1, r2] = await Promise.all([first, second]);

    assert.ok(r2.ok === false, 'no waiting queue — the second request is refused');
    if (!r2.ok) {
      assert.strictEqual(r2.error.code, 'LOG_FEEDBACK_BUSY');
      assert.strictEqual(r2.error.retryable, true);
    }
    assert.ok(r1.ok, 'the in-flight request completes normally');

    const third = await service.feedback('f-3', payload(dir));
    assert.ok(third.ok, 'a fresh request runs on the surviving worker');
  } finally {
    service.shutdown();
  }
});

test('a timed-out feedback worker is really dead — its thread stops, not just the promise', async () => {
  const dir = tempDir();
  const heartbeat = path.join(dir, 'heartbeat.log');
  process.env.FAKE_WORKER_HEARTBEAT = heartbeat;
  const service = new LogWorkerService({ workerPath: fakeWorkerPath(dir), feedbackTimeoutMs: 120 });
  try {
    const outcome = await service.feedback('f-hang', { ...payload(dir), hang: true } as FeedbackJobPayload);
    assert.ok(outcome.ok === false);
    if (!outcome.ok) {
      assert.strictEqual(outcome.error.code, 'LOG_FEEDBACK_TIMEOUT');
      assert.strictEqual(outcome.error.retryable, true);
    }

    // The heartbeat proves the thread ran; its flatline proves terminate() killed it.
    assert.ok(fs.existsSync(heartbeat), 'the worker thread actually started');
    const sizeAtKill = fs.statSync(heartbeat).size;
    await sleep(250);
    assert.strictEqual(fs.statSync(heartbeat).size, sizeAtKill, 'no heartbeats after the timeout kill');
  } finally {
    delete process.env.FAKE_WORKER_HEARTBEAT;
    service.shutdown();
  }
});

test('a feedback timeout never cancels a concurrent query or export', async () => {
  const dir = tempDir();
  const service = new LogWorkerService({
    workerPath: fakeWorkerPath(dir),
    feedbackTimeoutMs: 120,
    queryTimeoutMs: 10_000,
    exportTimeoutMs: 10_000,
  });
  try {
    const queried = service.query('q-1', { dir, from: 0, to: Date.now() });
    const exported = service.exportArchive('e-1', {
      dir,
      appVersion: '1.0.5',
      sessionId: SESS,
      from: 0,
      to: Date.now(),
    });
    const feedback = service.feedback('f-hang', { ...payload(dir), hang: true } as FeedbackJobPayload);
    const [rq, re, rf] = await Promise.all([queried, exported, feedback]);

    assert.ok(rf.ok === false && rf.error.code === 'LOG_FEEDBACK_TIMEOUT');
    assert.ok(rq.ok, 'query channel unaffected');
    assert.ok(re.ok, 'export channel unaffected');
  } finally {
    service.shutdown();
  }
});

test('a crashed feedback worker reports LOG_FEEDBACK_FAILED and the next request recovers', async () => {
  const dir = tempDir();
  const workerPath = fakeWorkerPath(dir);
  const service = new LogWorkerService({ workerPath, feedbackTimeoutMs: 10_000 });
  try {
    process.env.FAKE_WORKER_CRASH = '1';
    const crashed = await service.feedback('f-crash', payload(dir));
    assert.ok(crashed.ok === false);
    if (!crashed.ok) {
      // LOG_WORKER_CRASHED is internal — the contract only knows the five codes.
      assert.strictEqual(crashed.error.code, 'LOG_FEEDBACK_FAILED');
      assert.strictEqual(crashed.error.retryable, false);
    }

    delete process.env.FAKE_WORKER_CRASH;
    const recovered = await service.feedback('f-after-crash', payload(dir));
    assert.ok(recovered.ok, 'the channel spawns a fresh worker for the next request');
  } finally {
    delete process.env.FAKE_WORKER_CRASH;
    service.shutdown();
  }
});

test('a non-contract error code from the worker is normalized to LOG_FEEDBACK_FAILED', async () => {
  const dir = tempDir();
  const service = new LogWorkerService({ workerPath: fakeWorkerPath(dir), feedbackTimeoutMs: 10_000 });
  try {
    const outcome = await service.feedback('f-stray', {
      ...payload(dir),
      errorCode: 'LOG_QUERY_FAILED',
    } as FeedbackJobPayload);
    assert.ok(outcome.ok === false);
    if (!outcome.ok) assert.strictEqual(outcome.error.code, 'LOG_FEEDBACK_FAILED');
  } finally {
    service.shutdown();
  }
});

test('stale and duplicate worker messages are ignored', async () => {
  const dir = tempDir();
  const service = new LogWorkerService({ workerPath: fakeWorkerPath(dir), feedbackTimeoutMs: 10_000 });
  try {
    const outcome = await service.feedback('f-stale', {
      ...payload(dir),
      staleFirst: true,
      doubleReply: true,
      delayMs: 50,
    } as FeedbackJobPayload);
    assert.ok(outcome.ok, 'the real response resolves despite the bogus one before it');
    if (outcome.ok) assert.strictEqual(outcome.value.ok, true);
    await sleep(100); // give the duplicate reply time to arrive — it must be dropped silently
  } finally {
    service.shutdown();
  }
});

test('shutdown answers an in-flight feedback job as LOG_FEEDBACK_CANCELED', async () => {
  const dir = tempDir();
  const service = new LogWorkerService({ workerPath: fakeWorkerPath(dir), feedbackTimeoutMs: 10_000 });
  const running = service.feedback('f-cancel', { ...payload(dir), delayMs: 5000 } as FeedbackJobPayload);
  service.shutdown();

  const outcome = await running;
  assert.ok(outcome.ok === false);
  if (!outcome.ok) {
    assert.strictEqual(outcome.error.code, 'LOG_FEEDBACK_CANCELED');
    assert.strictEqual(outcome.error.retryable, false);
  }

  const after = await service.feedback('f-after-shutdown', payload(dir));
  assert.ok(after.ok === false);
  if (!after.ok) assert.strictEqual(after.error.code, 'LOG_FEEDBACK_CANCELED');
});
