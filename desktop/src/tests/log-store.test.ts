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

// --- Scale and long-text regressions -------------------------------------------------
// Each of these used to be a freeze: a 200 KiB plain line hit a quadratic file-name
// regex, a huge field was scanned in full, and a few hundred thousand entries were
// handed to `Array.prototype.push` via spread, which overflows the call stack.

test('a line over the 256 KiB ceiling is skipped, counted and never shown', () => {
  const dir = tempDir();
  const now = Date.now();
  const huge = 'y'.repeat(300 * 1024);
  fs.writeFileSync(
    path.join(dir, 'desktop.jsonl'),
    [
      JSON.stringify(entry({ event: 'before.huge', timestamp: new Date(now - 2000).toISOString() })),
      JSON.stringify({ ...entry({ event: 'huge.line' }), message: huge }),
      JSON.stringify(entry({ event: 'after.huge', timestamp: new Date(now).toISOString() })),
      '',
    ].join('\n')
  );

  const result = queryLogs({ dir, from: 0, to: now + 1000, sources: ['desktop'] });
  assert.strictEqual(result.skippedLines, 1, 'the oversized line is accounted for');
  assert.strictEqual(result.total, 2, 'the rest of the file is still served');
  assert.ok(
    !result.entries.some((e) => String(e.message).includes('yyyy')),
    'no unscrubbed prefix of the skipped line may reach the viewer'
  );
  assert.deepStrictEqual(
    result.entries.map((e) => e.event).sort(),
    ['after.huge', 'before.huge']
  );
});

test('an oversized field is replaced wholesale instead of being scrubbed in full', () => {
  const dir = tempDir();
  const now = Date.now();
  fs.writeFileSync(
    path.join(dir, 'desktop.jsonl'),
    `${JSON.stringify({
      ...entry({ timestamp: new Date(now).toISOString() }),
      message: 'tail-marker-abcdef',
      detail: 'd'.repeat(9000),
    })}\n`
  );

  const result = queryLogs({ dir, from: 0, to: now + 1000, sources: ['desktop'] });
  assert.strictEqual(result.total, 1);
  const detail = String(result.entries[0].detail);
  assert.match(detail, /omitted: oversized string/, 'replaced by the omission marker');
  assert.ok(!detail.includes('dddd'), 'not even a scrubbed prefix of the oversized field survives');
});

test('long separator-free text does not make the scrubber backtrack', () => {
  const dir = tempDir();
  const now = Date.now();
  // 200 KiB of dots and near-miss extensions: the old unbounded file-name pattern was
  // quadratic here, and this call ran on the UI thread.
  const pathological = `${'a.'.repeat(100 * 1024)}pdfx`;
  fs.writeFileSync(path.join(dir, 'desktop.jsonl'), `${pathological}\n`);

  const started = Date.now();
  const result = queryLogs({ dir, from: 0, to: now + 1000, sources: ['desktop'] });
  const elapsed = Date.now() - started;
  assert.strictEqual(result.total, 1);
  assert.ok(elapsed < 2000, `scrubbing stayed linear (took ${elapsed} ms)`);
});

test('hundreds of thousands of entries are appended without overflowing the stack', () => {
  const dir = tempDir();
  const base = Date.now();
  const total = 150_000;
  const lines = new Array<string>(total);
  for (let i = 0; i < total; i += 1) {
    lines[i] = JSON.stringify(
      entry({ timestamp: new Date(base + i).toISOString(), event: `bulk.${i}`, message: 'x' })
    );
  }
  fs.writeFileSync(path.join(dir, 'desktop.jsonl'), `${lines.join('\n')}\n`);

  const result = queryLogs({ dir, from: 0, to: base + total + 1000, sources: ['desktop'], page: 1 });
  assert.strictEqual(result.total, total);
  assert.strictEqual(result.entries.length, 200);
  assert.strictEqual(result.entries[0].event, `bulk.${total - 1}`, 'newest first');
  assert.strictEqual(result.hasMore, true);
});

test('a rotated file is still readable while the active file is being written', () => {
  const dir = tempDir();
  const now = Date.now();
  fs.writeFileSync(
    path.join(dir, 'desktop.jsonl.1'),
    `${JSON.stringify(entry({ timestamp: new Date(now - 10_000).toISOString(), event: 'rotated.older' }))}\n`
  );
  fs.writeFileSync(
    path.join(dir, 'desktop.jsonl'),
    `${JSON.stringify(entry({ timestamp: new Date(now).toISOString(), event: 'active.newer' }))}\n`
  );

  const result = queryLogs({ dir, from: 0, to: now + 1000, sources: ['desktop'] });
  assert.strictEqual(result.total, 2);
  assert.deepStrictEqual(
    result.entries.map((e) => e.event),
    ['active.newer', 'rotated.older'],
    'newest first across the rotation boundary'
  );
});

test('corrupt lines, CRLF endings and a missing source never fail the whole query', () => {
  const dir = tempDir();
  const now = Date.now();
  fs.writeFileSync(
    path.join(dir, 'desktop.jsonl'),
    [
      JSON.stringify(entry({ event: 'ok.crlf', timestamp: new Date(now).toISOString() })),
      '{"truncated": ',
      '',
      JSON.stringify(entry({ event: 'ok.after.corrupt', timestamp: new Date(now - 1000).toISOString() })),
    ].join('\r\n')
  );

  const result = queryLogs({
    dir,
    from: 0,
    to: now + 1000,
    sources: ['desktop', 'backend', 'frontend'],
  });

  assert.strictEqual(result.ok, true, 'one bad line cannot fail the query');
  assert.deepStrictEqual(result.unavailable, ['backend', 'frontend'], 'missing sources are reported, not fatal');
  assert.ok(result.entries.some((e) => e.event === 'ok.crlf'), 'CRLF endings are handled');
  assert.ok(result.entries.some((e) => e.event === 'ok.after.corrupt'), 'parsing resumes after a corrupt line');
  assert.ok(result.entries.some((e) => e.event === 'legacy.text'), 'the corrupt JSON line degrades to plain text');
});

test('the scan budget keeps the newest records and flags truncation', () => {
  const dir = tempDir();
  const base = Date.now();
  // Two rotated generations, each large enough that the active file alone cannot fit
  // under a deliberately small budget.
  const filler = (prefix: string, count: number, offset: number) =>
    Array.from({ length: count }, (_, i) =>
      JSON.stringify(
        entry({
          timestamp: new Date(base + offset + i).toISOString(),
          event: `${prefix}.${i}`,
          message: 'z'.repeat(400),
        })
      )
    ).join('\n');
  fs.writeFileSync(path.join(dir, 'desktop.jsonl'), `${filler('active', 400, 10_000)}\n`);
  fs.writeFileSync(path.join(dir, 'desktop.jsonl.1'), `${filler('rotated', 400, 0)}\n`);

  const result = queryLogs({
    dir,
    from: 0,
    to: base + 100_000,
    sources: ['desktop'],
    page: 1,
    pageSize: 500,
    maxScanBytes: 4096,
  });
  assert.strictEqual(result.scanTruncated, true, 'a partial read is reported');
  assert.ok(result.scannedBytes > 0 && result.scannedBytes <= 4096);
  assert.ok(result.total > 0, 'the newest records are still served');
  assert.ok(
    result.entries.length > 0 && result.entries[0].event.startsWith('active'),
    'newest-first ordering survives across rotated files'
  );
});

