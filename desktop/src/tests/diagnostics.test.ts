import test from 'node:test';
import assert from 'node:assert';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { DiagnosticsLogger, scrubRecord, scrubString } from '../diagnostics';

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
