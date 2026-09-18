import test from 'node:test';
import assert from 'node:assert';
import fs from 'fs';
import os from 'os';
import path from 'path';
import {
  DiagnosticsLogger,
  PENDING_MAX_BYTES,
  PENDING_MAX_ENTRIES,
  scrubRecord,
  scrubString,
} from '../diagnostics';

function tempDir(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'rag-diag-'));
}

function makeLogger(dir: string, overrides: Partial<ConstructorParameters<typeof DiagnosticsLogger>[0]> = {}) {
  return new DiagnosticsLogger({
    dir,
    source: 'desktop',
    sessionId: 'sess-test1234',
    appVersion: '1.0.1',
    aggregationWindowMs: 20,
    ...overrides,
  });
}

function readEntries(dir: string, file = 'desktop.jsonl'): any[] {
  const target = path.join(dir, file);
  if (!fs.existsSync(target)) return [];
  return fs
    .readFileSync(target, 'utf8')
    .split('\n')
    .filter((line) => line.trim())
    .map((line) => JSON.parse(line));
}

/** Polls a condition (default ~4s) — never assert on a fixed sleep for drains. */
async function waitFor(condition: () => boolean, timeoutMs = 4000): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (condition()) return true;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  return condition();
}

function jsonlBytes(record: Record<string, unknown>): number {
  return Buffer.byteLength(`${JSON.stringify(record)}\n`, 'utf8');
}

test('unified envelope: every record carries time/level/source/event/message/session/version', () => {
  const dir = tempDir();
  const logger = makeLogger(dir);
  logger.info('startup.app.ready', '桌面主进程就绪', { phase: 'ready' });
  logger.flushAggregated();

  const entries = readEntries(dir);
  assert.strictEqual(entries.length, 1);
  const entry = entries[0];
  assert.strictEqual(entry.source, 'desktop');
  assert.strictEqual(entry.level, 'INFO');
  assert.strictEqual(entry.event, 'startup.app.ready');
  assert.strictEqual(entry.message, '桌面主进程就绪');
  assert.strictEqual(entry.session_id, 'sess-test1234');
  assert.strictEqual(entry.app_version, '1.0.1');
  assert.strictEqual(entry.phase, 'ready');
  assert.ok(!Number.isNaN(Date.parse(entry.timestamp)), 'timestamp must be ISO-8601 parseable');
  assert.strictEqual(entry.platform, process.platform);
});

test('whitelist drops unknown fields so business payload can never be persisted', () => {
  const dir = tempDir();
  const logger = makeLogger(dir);
  logger.info('llm.call', 'call failed', {
    model: 'gpt-4o-mini',
    prompt: '用户的完整提示词内容',
    content: '资料原文',
    answer: '模型回答',
    document_text: '正文',
    ok: true,
  });
  logger.flushAggregated();

  const entry = readEntries(dir)[0];
  assert.strictEqual(entry.model, 'gpt-4o-mini');
  assert.strictEqual(entry.ok, true);
  for (const forbidden of ['prompt', 'content', 'answer', 'document_text']) {
    assert.strictEqual(entry[forbidden], undefined, `${forbidden} must be dropped`);
  }
  const raw = fs.readFileSync(path.join(dir, 'desktop.jsonl'), 'utf8');
  assert.ok(!raw.includes('用户的完整提示词内容'));
  assert.ok(!raw.includes('资料原文'));
});

test('redaction: secrets, URL query strings, user paths and file names', () => {
  // Real keys are masked.
  assert.match(scrubString('key sk-1234567890abcdef12345678'), /sk-1234\*\*\*/);
  // Identifier regression: a task id that merely contains "sk-" must survive intact.
  assert.strictEqual(scrubString('task-recon-999'), 'task-recon-999');
  assert.strictEqual(scrubString('task-1234567890'), 'task-1234567890');
  // URL params / fragments are stripped.
  assert.strictEqual(
    scrubString('GET https://api.example.com/v1/models?api-key=secret&user=bob'),
    'GET https://api.example.com/v1/models'
  );
  // User paths.
  assert.ok(!scrubString('C:\\Users\\测试用户\\AppData\\Local\\RAGStudyAssistant\\data\\app.db').includes('测试用户'));
  assert.ok(!scrubString('/Users/someone/Library/Application Support/RAGStudyAssistant/logs').includes('someone'));
  assert.ok(!scrubString('~/Documents/private/notes').includes('private'));
  // Document file names.
  assert.strictEqual(scrubString('failed to parse 2024真题.pdf'), 'failed to parse <file>');
  assert.strictEqual(scrubString('opened 复习笔记.docx'), 'opened <file>');
  // Source module names are diagnostics, not user content: they stay readable.
  assert.strictEqual(scrubString('ImportError in app/services/llm/client.py'), 'ImportError in app/services/llm/client.py');
});

test('recursive scrubbing applies to nested objects and arrays', () => {
  const scrubbed = scrubRecord({
    event: 'x',
    frames: [{ file: 'client.py', line: 12 }, { file: '2024真题.pdf', line: 3 }],
    unknown_nested: { content: '业务正文' },
  });
  assert.ok(Array.isArray(scrubbed.frames));
  const frames = scrubbed.frames as any[];
  assert.strictEqual(frames[0].file, 'client.py');
  assert.strictEqual(frames[1].file, '<file>');
  assert.strictEqual(scrubbed.unknown_nested, undefined);
});

test('rotation keeps at most backupCount files and restarts the active file', () => {
  const dir = tempDir();
  const logger = makeLogger(dir, { maxBytes: 400, backupCount: 2 });
  for (let i = 0; i < 40; i += 1) {
    logger.log('INFO', `rotate.event.${i}`, `message number ${i} with padding ................`);
    logger.flushAggregated();
  }

  assert.ok(fs.existsSync(path.join(dir, 'desktop.jsonl')), 'active file exists');
  assert.ok(fs.existsSync(path.join(dir, 'desktop.jsonl.1')), 'first rotation exists');
  assert.ok(!fs.existsSync(path.join(dir, 'desktop.jsonl.3')), 'never exceeds backupCount');
  const activeSize = fs.statSync(path.join(dir, 'desktop.jsonl')).size;
  assert.ok(activeSize <= 400 + 2048, `active file kept near the cap, got ${activeSize}`);
});

test('retention cleanup deletes rotated files older than the window and keeps recent ones', () => {
  const dir = tempDir();
  const logger = makeLogger(dir, { retentionDays: 7 });
  const stale = path.join(dir, 'desktop.jsonl.1');
  const fresh = path.join(dir, 'desktop.jsonl.2');
  fs.writeFileSync(stale, '{}\n');
  fs.writeFileSync(fresh, '{}\n');
  const oldTime = Date.now() / 1000 - 8 * 24 * 60 * 60;
  fs.utimesSync(stale, oldTime, oldTime);

  const removed = logger.cleanupOldFiles();
  assert.strictEqual(removed, 1);
  assert.ok(!fs.existsSync(stale), '8 day old rotated file removed');
  assert.ok(fs.existsSync(fresh), 'recent rotated file preserved');
});

test('rate limiting drops excess records and reports it once', () => {
  const dir = tempDir();
  const logger = makeLogger(dir, { maxRecordsPerSecond: 5 });
  for (let i = 0; i < 60; i += 1) {
    logger.log('INFO', `flood.event.${i}`, `flood ${i}`);
    logger.flushAggregated();
  }
  const entries = readEntries(dir);
  assert.ok(entries.length < 60, 'excess records were dropped');
  assert.ok(entries.length >= 5, 'at least the burst allowance was written');
  const notices = entries.filter((e) => e.event === 'log.rate_limited');
  assert.strictEqual(notices.length, 1, 'the drop notice is emitted exactly once per window');
  assert.ok(Number(notices[0].dropped) > 0);
});

test('duplicate errors aggregate into one summary carrying occurrences', () => {
  const dir = tempDir();
  const logger = makeLogger(dir, { aggregationWindowMs: 10_000 });
  for (let i = 0; i < 25; i += 1) {
    logger.error('llm.call_failed', '模型调用失败', { error_type: 'ConnectError' });
  }
  logger.flushAggregated();

  const entries = readEntries(dir);
  // 25 identical errors become 2 lines: the first occurrence in full (so the real error
  // is visible immediately) plus one aggregate summary.
  assert.strictEqual(entries.length, 2, `expected first + summary, got ${entries.length}`);
  assert.strictEqual(entries[0].occurrences, undefined, 'first occurrence is a plain record');
  assert.strictEqual(entries[1].occurrences, 25);
  assert.strictEqual(entries[1].event, 'llm.call_failed');
  assert.strictEqual(entries[1].error_type, 'ConnectError');
  assert.ok(typeof entries[1].first_seen === 'string' && typeof entries[1].last_seen === 'string');
});

test('a broken log directory degrades instead of throwing, and keeps an in-memory buffer', () => {
  const root = tempDir();
  // Occupy the intended log directory path with a FILE so mkdir/write must fail.
  const blocked = path.join(root, 'logs');
  fs.writeFileSync(blocked, 'not a directory');

  const logger = makeLogger(blocked);
  assert.doesNotThrow(() => logger.error('startup.backend.failed', '后台启动失败', { detail: 'boom' }));
  assert.doesNotThrow(() => logger.flushAggregated());
  assert.strictEqual(logger.isDegraded(), true);
  assert.ok(logger.getMemoryEntries().length >= 1, 'record retained in memory for export');
});

// --- beta.3 D2: recoverable pending-write queue ------------------------------

test('init failure queues records as pending (nothing on disk), recovery probe drains them once healed', async () => {
  const root = tempDir();
  const blocked = path.join(root, 'logs');
  fs.writeFileSync(blocked, 'not a directory');

  const logger = makeLogger(blocked, { recoveryIntervalMs: 20 });
  assert.strictEqual(logger.isDegraded(), true);

  logger.info('evt.queued1', 'first queued');
  logger.info('evt.queued2', 'second queued');
  const state = logger.getPersistenceState();
  assert.strictEqual(state.degraded, true);
  assert.strictEqual(state.pendingCount, 2);
  assert.ok(fs.statSync(blocked).isFile(), 'blocked path untouched: nothing reached disk');

  // Heal: remove the blocking file so the probe (mkdir + size read) can succeed.
  fs.unlinkSync(blocked);
  const recovered = await waitFor(
    () => !logger.getPersistenceState().degraded && logger.getPersistenceState().pendingCount === 0
  );
  assert.ok(recovered, 'pending queue drained after the log directory healed');

  const entries = readEntries(blocked);
  assert.deepStrictEqual(
    entries.map((e) => e.event),
    ['evt.queued1', 'evt.queued2', 'log.persistence.recovered']
  );
  assert.strictEqual(entries[2].entries, 2);
  assert.strictEqual(entries[2].dropped, 0);
  logger.close();
});

test('runtime write failure queues the failed record and later ones; auto-drains in order once healed', async () => {
  const dir = tempDir();
  const logger = makeLogger(dir, { recoveryIntervalMs: 20 });
  const file = path.join(dir, 'desktop.jsonl');

  logger.info('evt.before', 'already on disk');
  assert.strictEqual(readEntries(dir).length, 1);

  // Reliable cross-platform write failure: strip the write bit off the active
  // file (appendFileSync -> EACCES). stat still succeeds, so the probe is not
  // the failing step — the append inside drain is.
  fs.chmodSync(file, 0o444);
  logger.error('evt.fails', 'the record whose append fails', { error_type: 'DiskError' });
  assert.strictEqual(logger.isDegraded(), true);
  logger.info('evt.after1', 'queued behind the failure');
  logger.info('evt.after2', 'queued last');

  const pendingBefore = logger.getPendingEntries().map((e) => e.event);
  assert.deepStrictEqual(pendingBefore, ['evt.fails', 'evt.after1', 'evt.after2']);
  assert.strictEqual(readEntries(dir).length, 1, 'old disk record still there, nothing half-written');

  fs.chmodSync(file, 0o644);
  const drained = await waitFor(
    () => !logger.getPersistenceState().degraded && logger.getPersistenceState().pendingCount === 0
  );
  assert.ok(drained, 'queue drained without any new business event');

  const entries = readEntries(dir);
  // Disk order must equal pending order (queued records keep original sequence).
  assert.deepStrictEqual(
    entries.map((e) => e.event),
    ['evt.before', 'evt.fails', 'evt.after1', 'evt.after2', 'log.persistence.recovered']
  );
  const replayed = entries.find((e) => e.event === 'evt.fails');
  assert.strictEqual(replayed.error_type, 'DiskError', 'error fields preserved through replay');
  assert.strictEqual(replayed.session_id, 'sess-test1234');
  assert.strictEqual(replayed.level, 'ERROR');
  assert.ok(!Number.isNaN(Date.parse(replayed.timestamp)), 'original timestamp preserved');
  assert.match(String(replayed.log_record_id), /^sess-test1234:desktop:\d{8}$/);
  const summary = entries[entries.length - 1];
  assert.strictEqual(summary.entries, 3);
  assert.strictEqual(summary.dropped, 0);
  assert.match(String(summary.log_record_id), /^sess-test1234:desktop:\d{8}$/);
  logger.close();
});

test('repeated recovery failures keep the queue intact, stay degraded and never throw', async () => {
  const dir = tempDir();
  const logger = makeLogger(dir, { recoveryIntervalMs: 20 });
  const file = path.join(dir, 'desktop.jsonl');

  logger.info('evt.before', 'x');
  fs.chmodSync(file, 0o444);
  logger.info('evt.q1', 'q1');
  logger.info('evt.q2', 'q2');
  assert.strictEqual(logger.getPersistenceState().pendingCount, 2);

  // Several recovery ticks fire while the disk is still broken: each drain
  // attempt must fail at the first append and leave the rest queued.
  await new Promise((resolve) => setTimeout(resolve, 120));
  const state = logger.getPersistenceState();
  assert.strictEqual(state.degraded, true, 'still degraded after repeated failed attempts');
  assert.strictEqual(state.pendingCount, 2, 'nothing lost while retries keep failing');
  assert.strictEqual(readEntries(dir).length, 1);

  fs.chmodSync(file, 0o644);
  const drained = await waitFor(() => logger.getPersistenceState().pendingCount === 0);
  assert.ok(drained, 'a later attempt completes once the disk heals');
  assert.deepStrictEqual(
    readEntries(dir).map((e) => e.event),
    ['evt.before', 'evt.q1', 'evt.q2', 'log.persistence.recovered']
  );
  logger.close();
});

test('pending queue entry cap: oldest are evicted, droppedPending accumulates permanently', async () => {
  const dir = tempDir();
  const logger = makeLogger(dir, { recoveryIntervalMs: 20, maxRecordsPerSecond: 10000 });
  const file = path.join(dir, 'desktop.jsonl');

  logger.info('evt.seed', 'x');
  fs.chmodSync(file, 0o444);
  for (let i = 0; i < PENDING_MAX_ENTRIES + 100; i += 1) {
    logger.info(`evt.flood.${i}`, `m${i}`);
  }
  const state = logger.getPersistenceState();
  assert.strictEqual(state.pendingCount, PENDING_MAX_ENTRIES);
  assert.strictEqual(state.droppedPending, 100);
  const pending = logger.getPendingEntries();
  assert.strictEqual(pending[0].event, 'evt.flood.100', 'the oldest 100 were dropped');
  assert.strictEqual(pending[pending.length - 1].event, `evt.flood.${PENDING_MAX_ENTRIES + 99}`);

  fs.chmodSync(file, 0o644);
  const drained = await waitFor(() => logger.getPersistenceState().pendingCount === 0);
  assert.ok(drained, 'bounded queue drains once writable');
  const summary = readEntries(dir).find((e) => e.event === 'log.persistence.recovered');
  assert.ok(summary, 'recovered summary written after drain');
  assert.strictEqual(summary.entries, PENDING_MAX_ENTRIES);
  assert.strictEqual(summary.dropped, 100, 'summary reports the cumulative drop count');
  // droppedPending is permanent: draining does not reset it.
  assert.strictEqual(logger.getPersistenceState().droppedPending, 100);
  logger.close();
});

test('pending queue byte cap (1 MiB) binds before the entry cap for large records', () => {
  const dir = tempDir();
  const logger = makeLogger(dir, { recoveryIntervalMs: 60_000, maxRecordsPerSecond: 10000 });
  const file = path.join(dir, 'desktop.jsonl');

  logger.info('evt.seed', 'x');
  fs.chmodSync(file, 0o444);
  const big = 'x'.repeat(1900); // survives scrubbing just under the 2000-char cap
  for (let i = 0; i < 400; i += 1) {
    logger.info(`evt.big.${i}`, 'big record', { detail: big, reason: big, info: big });
  }
  const state = logger.getPersistenceState();
  assert.ok(state.pendingCount < 400, `byte cap evicted oldest, got ${state.pendingCount}`);
  assert.ok(state.pendingCount < PENDING_MAX_ENTRIES, 'byte cap bound before the entry cap');
  assert.strictEqual(state.droppedPending + state.pendingCount, 400, 'every record accounted for');
  const snapshotBytes = logger.getPendingEntries().reduce((sum, e) => sum + jsonlBytes(e), 0);
  assert.ok(snapshotBytes <= PENDING_MAX_BYTES, `queue stayed within 1 MiB, got ${snapshotBytes}`);

  fs.chmodSync(file, 0o644);
  logger.close();
});

test('close() drains the queue once in order when the disk works, then stops', () => {
  const dir = tempDir();
  const logger = makeLogger(dir, { recoveryIntervalMs: 60_000 });
  const file = path.join(dir, 'desktop.jsonl');

  logger.info('evt.before', 'x');
  fs.chmodSync(file, 0o444);
  logger.info('evt.q1', 'q1');
  logger.info('evt.q2', 'q2');
  assert.strictEqual(logger.getPersistenceState().pendingCount, 2);

  fs.chmodSync(file, 0o644);
  assert.doesNotThrow(() => logger.close());
  assert.deepStrictEqual(
    readEntries(dir).map((e) => e.event),
    ['evt.before', 'evt.q1', 'evt.q2'],
    'best-effort close drain lands queued records in order'
  );
  logger.info('evt.late', 'ignored after close');
  assert.strictEqual(readEntries(dir).length, 3, 'closed logger writes nothing more');
});

test('close() with the disk still broken swallows failures and stops the recovery timer', async () => {
  const dir = tempDir();
  const logger = makeLogger(dir, { recoveryIntervalMs: 20 });
  const file = path.join(dir, 'desktop.jsonl');

  logger.info('evt.before', 'x');
  fs.chmodSync(file, 0o444);
  logger.info('evt.stuck', 'cannot be written');
  assert.doesNotThrow(() => logger.close());
  assert.strictEqual(logger.getPersistenceState().pendingCount, 1, 'failed close-drain leaves the queue');

  fs.chmodSync(file, 0o644); // heal AFTER close — the timer must already be dead
  await new Promise((resolve) => setTimeout(resolve, 80));
  assert.strictEqual(logger.getPersistenceState().pendingCount, 1, 'no post-close replay: timer stopped');
  assert.strictEqual(readEntries(dir).length, 1, 'nothing written after close');
});

test('log_record_id: minted per persisted record, monotonic, whitelisted through scrubRecord', () => {
  const dir = tempDir();
  const logger = makeLogger(dir);
  logger.info('evt.a', 'a');
  logger.info('evt.b', 'b');
  const entries = readEntries(dir);
  assert.strictEqual(entries.length, 2);
  assert.match(String(entries[0].log_record_id), /^sess-test1234:desktop:\d{8}$/);
  assert.match(String(entries[1].log_record_id), /^sess-test1234:desktop:\d{8}$/);
  assert.ok(
    String(entries[1].log_record_id) > String(entries[0].log_record_id),
    'sequence is monotonically increasing (zero-padded)'
  );
  // The id rides along in the memory ring as well.
  assert.strictEqual(logger.getMemoryEntries()[0].log_record_id, entries[0].log_record_id);
  // Whitelist: a re-scrubbed record keeps its id (export/dedup path).
  assert.strictEqual(
    scrubRecord({ log_record_id: 'sess-x:desktop:00000009', event: 'e' }).log_record_id,
    'sess-x:desktop:00000009'
  );
  logger.close();

  // Aggregate summaries get a FRESH id — never the template's.
  const dir2 = tempDir();
  const logger2 = makeLogger(dir2, { aggregationWindowMs: 10_000 });
  for (let i = 0; i < 3; i += 1) logger2.error('evt.dup', '重复错误');
  logger2.flushAggregated();
  const entries2 = readEntries(dir2);
  assert.strictEqual(entries2.length, 2, 'first occurrence + one summary');
  assert.ok(entries2[0].log_record_id && entries2[1].log_record_id);
  assert.notStrictEqual(entries2[0].log_record_id, entries2[1].log_record_id);
  logger2.close();
});

test('getPendingEntries: ordered snapshot, isolated from the queue, only queued records', () => {
  const dir = tempDir();
  const logger = makeLogger(dir, { recoveryIntervalMs: 60_000 });
  const file = path.join(dir, 'desktop.jsonl');

  logger.info('evt.ondisk', 'old disk record');
  fs.chmodSync(file, 0o444);
  logger.info('evt.p1', 'p1');
  logger.info('evt.p2', 'p2');

  const snap = logger.getPendingEntries();
  // Disk records are NOT part of the snapshot even though the file already has data.
  assert.deepStrictEqual(snap.map((e) => e.event), ['evt.p1', 'evt.p2']);
  assert.match(String(snap[0].log_record_id), /^sess-test1234:desktop:\d{8}$/);

  // Mutating the snapshot must not affect the queue.
  snap[0].event = 'MUTATED';
  snap.push({ event: 'evt.fake' } as any);
  const snap2 = logger.getPendingEntries();
  assert.deepStrictEqual(snap2.map((e) => e.event), ['evt.p1', 'evt.p2']);

  fs.chmodSync(file, 0o644);
  logger.close();
});

test('getPersistenceState exposes exactly degraded / pendingCount / droppedPending', () => {
  const dir = tempDir();
  const logger = makeLogger(dir, { recoveryIntervalMs: 60_000 });
  assert.deepStrictEqual(Object.keys(logger.getPersistenceState()).sort(), [
    'degraded',
    'droppedPending',
    'pendingCount',
  ]);
  const file = path.join(dir, 'desktop.jsonl');
  logger.info('evt.x', 'x');
  fs.chmodSync(file, 0o444);
  logger.info('evt.y', 'y');
  const state = logger.getPersistenceState();
  assert.deepStrictEqual(state, { degraded: true, pendingCount: 1, droppedPending: 0 });
  fs.chmodSync(file, 0o644);
  logger.close();
});
