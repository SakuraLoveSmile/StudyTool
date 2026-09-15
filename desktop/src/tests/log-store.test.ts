import test from 'node:test';
import assert from 'node:assert';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { collectExportEntries, parseLogLine, queryLogs } from '../log-store';

function tempDir(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'rag-logs-'));
}

function writeLog(dir: string, source: string, entries: unknown[]): void {
  fs.writeFileSync(path.join(dir, `${source}.jsonl`), entries.map((e) => `${JSON.stringify(e)}\n`).join(''));
}

function entry(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    timestamp: new Date().toISOString(),
    level: 'INFO',
    source: 'desktop',
    event: 'test.event',
    message: 'hello',
    session_id: 'sess-1',
    app_version: '1.0.1',
    ...overrides,
  };
}

test('query filters by time, level, source, session, task id and keyword', () => {
  const dir = tempDir();
  const now = Date.now();
  writeLog(dir, 'desktop', [
    entry({ timestamp: new Date(now - 60_000).toISOString(), event: 'old.event' }),
    entry({ level: 'ERROR', event: 'startup.backend.failed', message: '后台启动失败', task_id: 'task-abc' }),
    entry({ level: 'WARN', event: 'startup.port.conflict', message: '端口冲突' }),
  ]);
  writeLog(dir, 'frontend', [
    entry({ source: 'frontend', level: 'ERROR', event: 'vue.error', message: '渲染错误', session_id: 'sess-2' }),
  ]);

  const all = queryLogs({ dir, from: now - 3600_000, to: now + 1000 });
  assert.strictEqual(all.total, 4);

  const errors = queryLogs({ dir, from: now - 3600_000, to: now + 1000, levels: ['ERROR'] });
  assert.strictEqual(errors.total, 2);
  assert.ok(errors.entries.every((e) => e.level === 'ERROR'));

  const frontendOnly = queryLogs({ dir, from: now - 3600_000, to: now + 1000, sources: ['frontend'] });
  assert.strictEqual(frontendOnly.total, 1);
  assert.strictEqual(frontendOnly.entries[0].source, 'frontend');

  const bySession = queryLogs({ dir, from: now - 3600_000, to: now + 1000, sessionId: 'sess-2' });
  assert.strictEqual(bySession.total, 1);

  const byTask = queryLogs({ dir, from: now - 3600_000, to: now + 1000, taskId: 'task-abc' });
  assert.strictEqual(byTask.total, 1);
  assert.strictEqual(byTask.entries[0].event, 'startup.backend.failed');

  const byKeyword = queryLogs({ dir, from: now - 3600_000, to: now + 1000, keyword: '端口' });
  assert.strictEqual(byKeyword.total, 1);

  const narrow = queryLogs({ dir, from: now - 30_000, to: now + 1000 });
  assert.strictEqual(narrow.total, 3, 'older record excluded by the time window');
});

test('query paginates newest-first with the 200 per page default', () => {
  const dir = tempDir();
  const base = Date.now();
  const many = Array.from({ length: 450 }, (_, i) =>
    entry({ timestamp: new Date(base - (450 - i) * 1000).toISOString(), event: `evt.${i}` })
  );
  writeLog(dir, 'desktop', many);

  const page1 = queryLogs({ dir, from: 0, to: Date.now() + 1000 });
  assert.strictEqual(page1.pageSize, 200);
  assert.strictEqual(page1.entries.length, 200);
  assert.strictEqual(page1.total, 450);
  assert.strictEqual(page1.hasMore, true);
  assert.strictEqual(page1.entries[0].event, 'evt.449', 'newest first');

  const page3 = queryLogs({ dir, from: 0, to: Date.now() + 1000, page: 3 });
  assert.strictEqual(page3.entries.length, 50);
  assert.strictEqual(page3.hasMore, false);
});

test('legacy records are normalized rather than dropped', () => {
  const dir = tempDir();
  fs.writeFileSync(
    path.join(dir, 'backend.jsonl'),
    [
      // Older JSON key names, no source/session fields at all.
      JSON.stringify({ asctime: '2026-09-01 10:00:00', levelname: 'WARNING', msg: '旧格式告警', name: 'app.old' }),
      // A seconds-precision numeric timestamp.
      JSON.stringify({ time: Math.floor(Date.now() / 1000) - 5, level: 'ERROR', message: '秒级时间戳' }),
      // A plain console line that is not JSON at all.
      'plain text console line about 2024真题.pdf',
      '',
    ].join('\n')
  );

  const result = queryLogs({ dir, from: 0, to: Date.now() + 1000, sources: ['backend'] });
  assert.strictEqual(result.total, 3, 'no legacy record is silently dropped');

  const warning = result.entries.find((e) => e.event === 'app.old')!;
  assert.strictEqual(warning.level, 'WARN', 'WARNING normalized to WARN');
  assert.strictEqual(warning.source, 'backend', 'source inferred from the file name');
  assert.strictEqual(warning.message, '旧格式告警');
  assert.ok(warning.timestamp_ms > 0);

  const seconds = result.entries.find((e) => e.message === '秒级时间戳')!;
  assert.ok(seconds.timestamp_ms > Date.now() - 60_000, 'seconds timestamp promoted to ms');

  const plain = result.entries.find((e) => e.event === 'legacy.text')!;
  assert.match(plain.message, /<file>/, 'file names scrubbed even on the legacy path');
});

test('entries are re-scrubbed on read, so old files cannot leak through export', () => {
  const dir = tempDir();
  fs.writeFileSync(
    path.join(dir, 'desktop.jsonl'),
    `${JSON.stringify({
      timestamp: new Date().toISOString(),
      level: 'ERROR',
      event: 'old.leak',
      message: 'key sk-1234567890abcdef token=abcdef123456',
      api_key: 'sk-abcdef1234567890',
      prompt: '这是一段业务提示词',
    })}\n`
  );

  const result = queryLogs({ dir, from: 0, to: Date.now() + 1000 });
  assert.strictEqual(result.total, 1);
  const raw = JSON.stringify(result.entries[0]);
  assert.ok(!raw.includes('abcdef123456'), 'secret masked on read');
  assert.ok(!raw.includes('这是一段业务提示词'), 'non-whitelisted field dropped on read');

  const exported = collectExportEntries({ dir, from: 0, to: Date.now() + 1000, sources: ['desktop'] });
  const payload = exported.files.map((f) => f.content).join('');
  assert.ok(!payload.includes('abcdef123456'), 'export payload is re-scrubbed');
  assert.ok(!payload.includes('这是一段业务提示词'));
});

test('export caps raw bytes, keeps the newest records and flags truncation', () => {
  const dir = tempDir();
  const base = Date.now();
  const entries = Array.from({ length: 200 }, (_, i) =>
    entry({
      timestamp: new Date(base - (200 - i) * 1000).toISOString(),
      event: `evt.${i}`,
      message: 'x'.repeat(200),
    })
  );
  writeLog(dir, 'desktop', entries);

  const selection = collectExportEntries({
    dir,
    from: 0,
    to: Date.now() + 1000,
    sources: ['desktop'],
    maxRawBytes: 4096,
  });

  assert.strictEqual(selection.truncated, true);
  assert.ok(selection.totalBytes <= 4096, `stayed within the cap (${selection.totalBytes})`);
  assert.ok(selection.totalEntries > 0 && selection.totalEntries < 200);
  assert.match(selection.note, /超过 4 MiB|超过 0 MiB|已仅保留最新记录/);
  // Newest records survive: the first exported line must be a high evt.N.
  const firstLine = selection.files[0].content.split('\n')[0];
  assert.match(firstLine, /evt\.19[0-9]/);
});

test('export groups payload per source and falls back to the memory buffer when disk is empty', () => {
  const dir = tempDir();
  writeLog(dir, 'backend', [entry({ source: 'backend', event: 'backend.event' })]);
  writeLog(dir, 'frontend', [entry({ source: 'frontend', event: 'frontend.event' })]);

  const selection = collectExportEntries({ dir, from: 0, to: Date.now() + 1000 });
  assert.deepStrictEqual(
    selection.files.map((f) => f.name).sort(),
    ['backend.jsonl', 'frontend.jsonl']
  );
  assert.strictEqual(selection.perSource.backend, 1);
  assert.strictEqual(selection.perSource.frontend, 1);

  // No desktop.jsonl on disk (log directory unwritable) → memory buffer is used.
  const memorySelection = collectExportEntries({
    dir,
    from: 0,
    to: Date.now() + 1000,
    memoryFallback: [entry({ event: 'startup.backend.failed', level: 'ERROR' })],
  });
  assert.ok(memorySelection.files.some((f) => f.name === 'desktop.jsonl'));
  assert.match(memorySelection.note, /内存缓冲/);
});

test('parseLogLine tolerates malformed JSON without throwing', () => {
  assert.doesNotThrow(() => parseLogLine('{"broken": ', 'desktop'));
  assert.doesNotThrow(() => parseLogLine('', 'desktop'));
  assert.strictEqual(parseLogLine('', 'desktop'), null);
  const parsed = parseLogLine('{"broken": ', 'desktop');
  assert.strictEqual(parsed!.event, 'legacy.text');
});
