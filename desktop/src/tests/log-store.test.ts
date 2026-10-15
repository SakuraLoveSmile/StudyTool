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
  assert.strictEqual(selection.incomplete, true, 'capacity truncation marks the export incomplete');
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

test('query surfaces permission and IO failures instead of reporting an unavailable source', () => {
  const cases: Array<{ stage: 'stat' | 'open' | 'read'; code: string }> = [
    { stage: 'stat', code: 'EACCES' },
    { stage: 'open', code: 'EPERM' },
    { stage: 'read', code: 'EIO' },
  ];

  for (const failure of cases) {
    const dir = tempDir();
    const active = path.join(dir, 'desktop.jsonl');
    writeLog(dir, 'desktop', [entry({ event: `failure.${failure.stage}` })]);
    const originalStat = fs.statSync;
    const originalOpen = fs.openSync;
    const originalRead = fs.readSync;
    const fault = (): never => {
      const error = new Error(failure.code) as NodeJS.ErrnoException;
      error.code = failure.code;
      throw error;
    };
    try {
      if (failure.stage === 'stat') {
        (fs as any).statSync = ((file: fs.PathLike) => {
          if (String(file) === active) return fault();
          return originalStat(file);
        }) as typeof fs.statSync;
      } else if (failure.stage === 'open') {
        (fs as any).openSync = ((file: fs.PathLike, ...args: any[]) => {
          if (String(file) === active) return fault();
          return (originalOpen as any)(file, ...args);
        }) as typeof fs.openSync;
      } else {
        (fs as any).readSync = ((..._args: any[]) => fault()) as typeof fs.readSync;
      }

      assert.throws(
        () => queryLogs({ dir, from: 0, to: Date.now() + 1000, sources: ['desktop'] }),
        (error: any) => {
          assert.strictEqual(error.code, failure.code);
          assert.match(error.message, /日志读取失败：desktop/);
          if (failure.code === 'EACCES' || failure.code === 'EPERM') {
            assert.match(error.message, /权限被拒绝/);
          } else {
            assert.match(error.message, /读取失败/);
          }
          assert.ok(!error.message.includes(dir), 'raw paths must not cross the query boundary');
          return true;
        }
      );
    } finally {
      (fs as any).statSync = originalStat;
      (fs as any).openSync = originalOpen;
      (fs as any).readSync = originalRead;
    }

    const recovered = queryLogs({ dir, from: 0, to: Date.now() + 1000, sources: ['desktop'] });
    assert.strictEqual(recovered.total, 1, `${failure.stage} failure must be retryable after recovery`);
  }
});

test('a missing active file is unavailable only when no rotated generation exists', () => {
  const dir = tempDir();
  const rotated = path.join(dir, 'desktop.jsonl.1');
  fs.writeFileSync(rotated, `${JSON.stringify(entry({ event: 'rotated.only' }))}\n`);
  const result = queryLogs({ dir, from: 0, to: Date.now() + 1000, sources: ['desktop'] });
  assert.strictEqual(result.total, 1);
  assert.deepStrictEqual(result.unavailable, []);

  fs.rmSync(rotated);
  const missing = queryLogs({ dir, from: 0, to: Date.now() + 1000, sources: ['desktop'] });
  assert.strictEqual(missing.total, 0);
  assert.deepStrictEqual(missing.unavailable, ['desktop']);
});

test('empty files still require read permission, and unscanned sources are not missing', (t) => {
  const dir = tempDir();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  fs.writeFileSync(path.join(dir, 'desktop.jsonl'), '');
  const open = t.mock.method(fs, 'openSync', () => {
    throw Object.assign(new Error('private path'), { code: 'EACCES' });
  });
  assert.throws(() => queryLogs({ dir, sources: ['desktop'] }), /读取失败.*EACCES/);
  open.mock.restore();
  writeLog(dir, 'desktop', [entry()]);
  writeLog(dir, 'backend', [entry({ source: 'backend' })]);
  const truncated = queryLogs({ dir, sources: ['desktop', 'backend'], maxScanBytes: 1 });
  assert.strictEqual(truncated.scanTruncated, true);
  assert.deepStrictEqual(truncated.unavailable, []);
});

test('export keeps newest-first partial results when a rotated file cannot be read', (t) => {
  const dir = tempDir();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  writeLog(dir, 'desktop', [
    entry({ timestamp: new Date(1000).toISOString(), event: 'older' }),
    entry({ timestamp: new Date(2000).toISOString(), event: 'newer' }),
  ]);
  fs.mkdirSync(path.join(dir, 'desktop.jsonl.1'));
  assert.throws(() => queryLogs({ dir, sources: ['desktop'] }), /读取失败/);
  const exported = collectExportEntries({ dir, sources: ['desktop'], from: 0, to: 3000 });
  assert.strictEqual(JSON.parse(exported.files[0].content.split('\n')[0]).event, 'newer');
});

test('export continues through unreadable generations while queries still fail', (t) => {
  const dir = tempDir();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const active = path.join(dir, 'desktop.jsonl');
  fs.writeFileSync(active, '');
  const originalOpen = fs.openSync;
  t.mock.method(fs, 'openSync', (file: fs.PathLike, flags: fs.OpenMode, mode?: fs.Mode) => {
    if (String(file) === active) throw Object.assign(new Error('denied'), { code: 'EACCES' });
    return originalOpen(file, flags, mode);
  });
  fs.mkdirSync(path.join(dir, 'desktop.jsonl.2'));
  fs.writeFileSync(path.join(dir, 'desktop.jsonl.1'), `${JSON.stringify(entry({ event: 'history.one' }))}\n`);
  fs.writeFileSync(path.join(dir, 'desktop.jsonl.3'), `${JSON.stringify(entry({ event: 'history.three' }))}\n`);
  assert.throws(() => queryLogs({ dir, sources: ['desktop'] }), /读取失败/);
  const exported = collectExportEntries({ dir, sources: ['desktop'], from: 0, to: Date.now() + 1000 });
  assert.strictEqual(exported.totalEntries, 2);
  assert.match(exported.files[0].content, /history.one/);
  assert.match(exported.files[0].content, /history.three/);
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

// --- Export incompleteness disclosure (beta.3 D3) -------------------------------------
// The export must never silently ship a partial bundle: every read failure, missing
// source, memory-sourced record and dropped pending entry is a first-class field.

test('export discloses an unreadable active generation while history still ships', (t) => {
  const dir = tempDir();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const now = Date.now();
  const active = path.join(dir, 'desktop.jsonl');
  fs.writeFileSync(active, `${JSON.stringify(entry({ event: 'active.new', timestamp: new Date(now).toISOString() }))}\n`);
  fs.writeFileSync(
    path.join(dir, 'desktop.jsonl.1'),
    `${JSON.stringify(entry({ event: 'rotated.old', timestamp: new Date(now - 10_000).toISOString() }))}\n`
  );
  const originalOpen = fs.openSync;
  t.mock.method(fs, 'openSync', (file: fs.PathLike, flags: fs.OpenMode, mode?: fs.Mode) => {
    if (String(file) === active) throw Object.assign(new Error('denied-with-secret-path'), { code: 'EACCES' });
    return originalOpen(file, flags, mode);
  });

  const selection = collectExportEntries({ dir, sources: ['desktop'], from: 0, to: now + 1000 });
  assert.deepStrictEqual(
    selection.read_errors,
    [{ source: 'desktop', file: 'desktop.jsonl', stage: 'open', code: 'EACCES' }],
    'the failed generation is disclosed with source + basename + stage + code'
  );
  assert.deepStrictEqual(selection.missing_sources, []);
  const payload = selection.files.map((f) => f.content).join('\n');
  assert.match(payload, /rotated\.old/, 'readable rotated history is still exported');
  assert.ok(!payload.includes('active.new'), 'the unreadable generation contributes nothing');
  assert.strictEqual(selection.incomplete, true);
  assert.match(selection.note, /读取失败/);
  // Disclosure stays sanitized: relative names only, never absolute paths or raw
  // exception text, and certainly no log-line content.
  const disclosure = JSON.stringify(selection.read_errors);
  assert.ok(!disclosure.includes(dir), 'no absolute path in read_errors');
  assert.ok(!disclosure.includes('denied-with-secret-path'), 'no raw exception text in read_errors');
});

test('export discloses an unreadable middle generation without losing the rest', (t) => {
  const dir = tempDir();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const now = Date.now();
  fs.writeFileSync(path.join(dir, 'desktop.jsonl'), `${JSON.stringify(entry({ event: 'gen.active' }))}\n`);
  fs.writeFileSync(path.join(dir, 'desktop.jsonl.1'), `${JSON.stringify(entry({ event: 'gen.one' }))}\n`);
  // A directory squatting on generation .2 fails stat with EINVAL — a real Windows case.
  fs.mkdirSync(path.join(dir, 'desktop.jsonl.2'));
  fs.writeFileSync(path.join(dir, 'desktop.jsonl.3'), `${JSON.stringify(entry({ event: 'gen.three' }))}\n`);

  const selection = collectExportEntries({ dir, sources: ['desktop'], from: 0, to: now + 1000 });
  assert.deepStrictEqual(selection.read_errors, [
    { source: 'desktop', file: 'desktop.jsonl.2', stage: 'stat', code: 'EINVAL' },
  ]);
  assert.deepStrictEqual(selection.missing_sources, []);
  const payload = selection.files[0].content;
  for (const event of ['gen.active', 'gen.one', 'gen.three']) {
    assert.match(payload, new RegExp(event), `${event} survived the broken generation`);
  }
  assert.strictEqual(selection.incomplete, true);
});

test('a source whose generations are all absent reports missing_sources, not errors', (t) => {
  const dir = tempDir();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const selection = collectExportEntries({ dir, sources: ['desktop'], from: 0, to: Date.now() + 1000 });
  assert.deepStrictEqual(selection.read_errors, []);
  assert.deepStrictEqual(selection.missing_sources, ['desktop']);
  assert.strictEqual(selection.files.length, 0, 'nothing to ship for an absent source');
  assert.strictEqual(selection.incomplete, false, 'absent logs are normal, not incomplete');
  assert.match(selection.note, /无磁盘日志文件：desktop/);
});

test('an existing empty file is neither missing nor a read error', (t) => {
  const dir = tempDir();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  fs.writeFileSync(path.join(dir, 'desktop.jsonl'), '');
  const selection = collectExportEntries({ dir, sources: ['desktop'], from: 0, to: Date.now() + 1000 });
  assert.deepStrictEqual(selection.read_errors, []);
  assert.deepStrictEqual(selection.missing_sources, []);
  assert.strictEqual(selection.incomplete, false);
});

test('scan-budget exhaustion is disclosed as read_truncated and marks the export incomplete', (t) => {
  const dir = tempDir();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const base = Date.now();
  const filler = Array.from({ length: 400 }, (_, i) =>
    JSON.stringify(
      entry({ timestamp: new Date(base + i).toISOString(), event: `bulk.${i}`, message: 'z'.repeat(400) })
    )
  ).join('\n');
  fs.writeFileSync(path.join(dir, 'desktop.jsonl'), `${filler}\n`);

  const selection = collectExportEntries({
    dir,
    sources: ['desktop'],
    from: 0,
    to: base + 1_000_000,
    maxScanBytes: 4096,
  });
  assert.strictEqual(selection.read_truncated, true, 'the scan hit its byte budget');
  assert.strictEqual(selection.truncated, false, 'the 20 MiB export cap was not reached');
  assert.deepStrictEqual(selection.read_errors, [], 'a truncated scan is not a read error');
  assert.ok(selection.totalEntries > 0, 'the newest records still ship');
  assert.strictEqual(selection.incomplete, true);
  assert.match(selection.note, /未完整读取|未读取/);
});

test('oversized lines surface as skipped_lines and mark the export incomplete', (t) => {
  const dir = tempDir();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const now = Date.now();
  fs.writeFileSync(
    path.join(dir, 'desktop.jsonl'),
    [
      JSON.stringify(entry({ event: 'kept', timestamp: new Date(now).toISOString() })),
      JSON.stringify({ ...entry({ event: 'huge' }), message: 'y'.repeat(300 * 1024) }),
      '',
    ].join('\n')
  );

  const selection = collectExportEntries({ dir, sources: ['desktop'], from: 0, to: now + 1000 });
  assert.strictEqual(selection.skippedLines, 1);
  assert.strictEqual(selection.totalEntries, 1);
  assert.strictEqual(selection.incomplete, true);
  assert.match(selection.note, /超长记录/);
});

test('pending records merge per source, dedupe by log_record_id and count as buffered', (t) => {
  const dir = tempDir();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const now = Date.now();
  writeLog(dir, 'desktop', [
    entry({
      event: 'disk.persisted',
      timestamp: new Date(now - 5000).toISOString(),
      log_record_id: 'sess-1:desktop:00000001',
    }),
  ]);

  const selection = collectExportEntries({
    dir,
    sources: ['desktop', 'backend'],
    from: now - 60_000,
    to: now + 1000,
    pendingEntries: [
      // Same id as the persisted record: the queued copy must be dropped.
      entry({ event: 'pending.dup', log_record_id: 'sess-1:desktop:00000001' }),
      // New queued records are kept; the newest must sort above the disk entry.
      entry({
        event: 'pending.fresh',
        timestamp: new Date(now).toISOString(),
        log_record_id: 'sess-1:desktop:00000002',
      }),
      // No id at all: always kept — better a duplicate than a gap.
      entry({ event: 'pending.noid', timestamp: new Date(now - 1000).toISOString() }),
      // Another source merges into its own payload file.
      entry({
        source: 'backend',
        event: 'pending.backend',
        timestamp: new Date(now).toISOString(),
        log_record_id: 'sess-1:backend:00000007',
      }),
      // Outside the export window: filtered, and not counted as buffered.
      entry({
        event: 'pending.expired',
        timestamp: new Date(now - 3_600_000).toISOString(),
        log_record_id: 'sess-1:desktop:00000003',
      }),
    ],
  });

  const desktop = selection.files.find((f) => f.name === 'desktop.jsonl')!;
  const events = desktop.content
    .trim()
    .split('\n')
    .map((line) => JSON.parse(line).event);
  assert.strictEqual(events[0], 'pending.fresh', 'merged list stays newest-first');
  assert.deepStrictEqual(
    [...events].sort(),
    ['disk.persisted', 'pending.fresh', 'pending.noid'],
    'deduped id dropped, id-less record kept, expired record filtered'
  );
  const backend = selection.files.find((f) => f.name === 'backend.jsonl')!;
  assert.match(backend.content, /pending\.backend/);
  assert.strictEqual(selection.buffered_entries, 3, 'only written pending records count');
  assert.deepStrictEqual(selection.memory_sources.sort(), ['backend', 'desktop']);
  assert.strictEqual(selection.incomplete, true);
  assert.match(selection.note, /3 条尚未落盘的内存待写记录/);
});

test('memory fallback and pending queue dedupe across both buffers', (t) => {
  const dir = tempDir();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const now = Date.now();
  const selection = collectExportEntries({
    dir,
    sources: ['desktop'],
    from: 0,
    to: now + 1000,
    memoryFallback: [entry({ event: 'memory.ring', log_record_id: 'sess-9:desktop:00000010' })],
    pendingEntries: [
      // Same id as the memory-ring record: dropped on merge.
      entry({ event: 'pending.same', log_record_id: 'sess-9:desktop:00000010' }),
      entry({ event: 'pending.only', log_record_id: 'sess-9:desktop:00000011' }),
    ],
  });

  const events = selection.files[0].content
    .trim()
    .split('\n')
    .map((line) => JSON.parse(line).event)
    .sort();
  assert.deepStrictEqual(events, ['memory.ring', 'pending.only']);
  assert.strictEqual(selection.buffered_entries, 1);
  assert.deepStrictEqual(selection.memory_sources, ['desktop']);
  assert.strictEqual(selection.incomplete, true);
  assert.match(selection.note, /内存缓冲/);
});

test('dropped_pending_entries passes through and marks the export incomplete', (t) => {
  const dir = tempDir();
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  writeLog(dir, 'desktop', [entry()]);

  const clean = collectExportEntries({ dir, sources: ['desktop'], from: 0, to: Date.now() + 1000 });
  assert.strictEqual(clean.incomplete, false);
  assert.strictEqual(clean.dropped_pending_entries, 0);
  assert.strictEqual(clean.buffered_entries, 0);
  assert.deepStrictEqual(clean.read_errors, []);
  assert.deepStrictEqual(clean.memory_sources, []);

  const dropped = collectExportEntries({
    dir,
    sources: ['desktop'],
    from: 0,
    to: Date.now() + 1000,
    droppedPendingEntries: 4,
  });
  assert.strictEqual(dropped.dropped_pending_entries, 4);
  assert.strictEqual(dropped.incomplete, true);
  assert.match(dropped.note, /4 条待写记录因缓冲超限被丢弃/);
});
