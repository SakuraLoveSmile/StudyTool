/**
 * Regression tests for the worker isolation layer.
 *
 * These assert the properties the freeze fix actually depends on: the main thread never
 * reads logs itself, only one job runs per channel, a waiting job is replaced rather
 * than queued, cancellation and timeout recycle the thread instead of hanging, and a
 * crashed worker degrades into a retryable error instead of a stuck window.
 */

import test from 'node:test';
import assert from 'node:assert';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { LogWorkerService, defaultWorkerPath, unpackedWorkerPath } from '../log-workers';

function tempDir(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'rag-workers-'));
}

function writeLog(dir: string, source: string, entries: unknown[]): void {
  fs.writeFileSync(
    path.join(dir, `${source}.jsonl`),
    entries.map((e) => `${JSON.stringify(e)}\n`).join('')
  );
}

function entry(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    timestamp: new Date().toISOString(),
    level: 'INFO',
    source: 'desktop',
    event: 'test.event',
    message: 'hello',
    session_id: 'sess-1',
    ...overrides,
  };
}

function queryOptions(dir: string): { dir: string; sources: string[]; from: number; to: number } {
  return { dir, sources: ['desktop'], from: 0, to: Date.now() + 60_000 };
}

test('a viewer query is executed by the worker, not by the caller', async () => {
  const dir = tempDir();
  writeLog(dir, 'desktop', [entry({ event: 'w.one' }), entry({ event: 'w.two' })]);

  const service = new LogWorkerService();
  try {
    const outcome = await service.query('q-ok', queryOptions(dir));
    assert.ok(outcome.ok, 'query succeeded');
    if (!outcome.ok) return;
    assert.strictEqual(outcome.value.total, 2);
    assert.strictEqual(outcome.value.pageSize, 200);
    assert.ok(outcome.value.entries.every((e) => typeof e.record_id === 'string'));
    assert.deepStrictEqual(
      outcome.value.entries.map((e) => e.record_id).sort(),
      ['desktop#0#1', 'desktop#0#2']
    );
  } finally {
    service.shutdown();
  }
});

test('filesystem failures cross the real worker boundary as errors and recover after repair', async () => {
  const root = tempDir();
  const dir = path.join(root, 'logs');
  // A file in place of the log directory reliably produces ENOTDIR on all hosts.
  fs.writeFileSync(dir, 'not a directory');
  const service = new LogWorkerService();
  try {
    const failed = await service.query('q-read-failed', queryOptions(dir));
    assert.strictEqual(failed.ok, false, 'a filesystem error must not become zero records');
    if (!failed.ok) {
      assert.strictEqual(failed.error.code, 'LOG_QUERY_FAILED');
      assert.strictEqual(failed.error.retryable, false, 'repair is required before a manual retry');
      assert.match(failed.error.message, /读取失败/);
      assert.ok(!failed.error.message.includes(root), 'system paths are not exposed');
    }
    fs.unlinkSync(dir);
    fs.mkdirSync(dir);
    writeLog(dir, 'desktop', [entry({ event: 'read.recovered' })]);
    const recovered = await service.query('q-read-recovered', queryOptions(dir));
    assert.ok(recovered.ok);
    if (recovered.ok) assert.strictEqual(recovered.value.entries[0].event, 'read.recovered');
  } finally {
    service.shutdown();
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('a denied log directory reports permission failure through the worker, then recovers', {
  skip: process.platform === 'win32' || process.getuid?.() === 0,
}, async () => {
  const dir = tempDir();
  writeLog(dir, 'desktop', [entry({ event: 'permission.recovered' })]);
  const service = new LogWorkerService();
  try {
    fs.chmodSync(dir, 0o000);
    const failed = await service.query('q-permission-denied', queryOptions(dir));
    assert.strictEqual(failed.ok, false);
    if (!failed.ok) {
      assert.strictEqual(failed.error.code, 'LOG_QUERY_FAILED');
      assert.match(failed.error.message, /EACCES|EPERM/);
      assert.match(failed.error.message, /权限/);
      assert.ok(!failed.error.message.includes(dir));
    }
    fs.chmodSync(dir, 0o700);
    const recovered = await service.query('q-permission-restored', queryOptions(dir));
    assert.ok(recovered.ok);
    if (recovered.ok) assert.strictEqual(recovered.value.total, 1);
  } finally {
    fs.chmodSync(dir, 0o700);
    service.shutdown();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('only the newest waiting query is kept; the replaced one is answered as superseded', async () => {
  const dir = tempDir();
  writeLog(dir, 'desktop', [entry()]);

  const service = new LogWorkerService();
  try {
    // submit() starts the first job synchronously, so the roles are deterministic:
    // q-1 runs, q-2 waits, q-3 replaces q-2.
    const first = service.query('q-1', queryOptions(dir));
    const second = service.query('q-2', queryOptions(dir));
    const third = service.query('q-3', queryOptions(dir));

    const [r1, r2, r3] = await Promise.all([first, second, third]);
    assert.ok(r2.ok === false, 'the replaced request must not be executed');
    if (!r2.ok) {
      assert.strictEqual(r2.error.code, 'LOG_QUERY_SUPERSEDED');
      assert.strictEqual(r2.error.retryable, true);
    }
    assert.ok(r1.ok, 'the running request still completes');
    assert.ok(r3.ok, 'the newest request completes');
  } finally {
    service.shutdown();
  }
});

test('cancelling a running query kills the worker and answers cancelled', async () => {
  const dir = tempDir();
  writeLog(dir, 'desktop', [entry()]);

  const service = new LogWorkerService();
  try {
    const running = service.query('q-cancel', queryOptions(dir));
    assert.strictEqual(service.cancelQuery('q-cancel'), true);

    const outcome = await running;
    assert.ok(outcome.ok === false);
    if (!outcome.ok) {
      assert.strictEqual(outcome.error.code, 'LOG_QUERY_CANCELED');
      assert.strictEqual(outcome.error.retryable, false);
    }

    // The channel must still serve the next request on a fresh worker.
    const next = await service.query('q-after-cancel', queryOptions(dir));
    assert.ok(next.ok, 'a new query works after the worker was recycled');
    assert.strictEqual(service.cancelQuery('q-cancel'), false, 'an unknown request is not cancellable');
  } finally {
    service.shutdown();
  }
});

test('cancelling a still-waiting query answers cancelled without running it', async () => {
  const dir = tempDir();
  writeLog(dir, 'desktop', [entry()]);

  const service = new LogWorkerService();
  try {
    const running = service.query('q-1', queryOptions(dir));
    const waiting = service.query('q-2', queryOptions(dir));
    assert.strictEqual(service.cancelQuery('q-2'), true);

    const outcome = await waiting;
    assert.ok(outcome.ok === false);
    if (!outcome.ok) assert.strictEqual(outcome.error.code, 'LOG_QUERY_CANCELED');
    assert.ok((await running).ok);
  } finally {
    service.shutdown();
  }
});

test('a query that exceeds its budget fails with a retryable timeout', async () => {
  const dir = tempDir();
  writeLog(dir, 'desktop', [entry()]);

  // 1 ms cannot cover even spawning the thread, so this is a deterministic timeout.
  const service = new LogWorkerService({ queryTimeoutMs: 1 });
  try {
    const outcome = await service.query('q-slow', queryOptions(dir));
    assert.ok(outcome.ok === false);
    if (!outcome.ok) {
      assert.strictEqual(outcome.error.code, 'LOG_QUERY_TIMEOUT');
      assert.strictEqual(outcome.error.retryable, true);
      assert.match(outcome.error.message, /可重试/);
    }
  } finally {
    service.shutdown();
  }
});

test('a crashed worker surfaces a retryable error instead of hanging', async () => {
  const dir = tempDir();
  const workerPath = path.join(dir, 'crash-worker.js');
  fs.writeFileSync(workerPath, 'process.exit(1);\n');

  const service = new LogWorkerService({ workerPath, queryTimeoutMs: 5000 });
  try {
    const outcome = await service.query('q-crash', queryOptions(dir));
    assert.ok(outcome.ok === false);
    if (!outcome.ok) {
      assert.strictEqual(outcome.error.code, 'LOG_WORKER_CRASHED');
      assert.strictEqual(outcome.error.retryable, true);
    }
  } finally {
    service.shutdown();
  }
});

test('a worker entry that cannot be spawned fails fast, not by hanging', async () => {
  const service = new LogWorkerService({
    workerPath: path.join(os.tmpdir(), 'rag-workers-definitely-missing', 'log-worker.js'),
    queryTimeoutMs: 5000,
  });
  try {
    const outcome = await service.query('q-missing', queryOptions(tempDir()));
    assert.ok(outcome.ok === false);
    if (!outcome.ok) assert.strictEqual(outcome.error.code, 'LOG_WORKER_CRASHED');
  } finally {
    service.shutdown();
  }
});

test('export runs in its own worker and refuses a second concurrent export', async () => {
  const dir = tempDir();
  writeLog(dir, 'desktop', [entry({ event: 'export.me' })]);

  const service = new LogWorkerService();
  const options = {
    dir,
    appVersion: '1.0.4',
    sessionId: 'sess-test',
    from: 0,
    to: Date.now() + 60_000,
  };
  try {
    const first = service.exportArchive('e-1', options);
    const second = service.exportArchive('e-2', options);
    const [r1, r2] = await Promise.all([first, second]);

    assert.ok(r2.ok === false, 'the second export must not be queued');
    if (!r2.ok) assert.strictEqual(r2.error.code, 'LOG_EXPORT_BUSY');

    assert.ok(r1.ok, 'the first export completes');
    if (!r1.ok) return;
    const buffer = Buffer.from(r1.value.buffer as unknown as Uint8Array);
    assert.strictEqual(buffer.readUInt32LE(0), 0x04034b50, 'a real ZIP local header');
    assert.strictEqual(r1.value.entries, 1);
  } finally {
    service.shutdown();
  }
});

test('an export does not block a query: the two channels are independent', async () => {
  const dir = tempDir();
  writeLog(dir, 'desktop', [entry({ event: 'parallel.me' })]);

  const service = new LogWorkerService();
  try {
    const exported = service.exportArchive('e-1', {
      dir,
      appVersion: '1.0.4',
      sessionId: 'sess-test',
      from: 0,
      to: Date.now() + 60_000,
    });
    const queried = service.query('q-1', queryOptions(dir));
    const [rExport, rQuery] = await Promise.all([exported, queried]);
    assert.ok(rExport.ok, 'export finished while a query was running');
    assert.ok(rQuery.ok, 'query finished while an export was running');
  } finally {
    service.shutdown();
  }
});

test('shutdown releases the workers and answers outstanding jobs', async () => {
  const dir = tempDir();
  writeLog(dir, 'desktop', [entry()]);

  const service = new LogWorkerService();
  const running = service.query('q-1', queryOptions(dir));
  service.shutdown();

  const outcome = await running;
  assert.ok(outcome.ok === false);
  if (!outcome.ok) assert.strictEqual(outcome.error.code, 'LOG_QUERY_CANCELED');
});

test('the packaged worker path points at an unpacked, existing file', () => {
  const resolved = defaultWorkerPath();
  assert.match(resolved, /log-worker\.js$/);
  assert.ok(!resolved.includes('app.asar' + path.sep), 'workers cannot load from inside app.asar');
  assert.ok(fs.existsSync(resolved), `compiled worker missing: ${resolved}`);
});

test('a Windows asar path is rewritten to the unpacked copy the installer ships', () => {
  // This is the exact shape a packaged build produces on the target platform; the
  // rewrite runs on the packaged path, not on this machine's layout.
  const packed = `C:${path.sep}Users${path.sep}u${path.sep}AppData${path.sep}Local${path.sep}Programs${path.sep}RAG${path.sep}resources${path.sep}app.asar${path.sep}dist${path.sep}log-worker.js`;
  const unpacked = unpackedWorkerPath(packed);
  assert.ok(
    unpacked.includes(`${path.sep}app.asar.unpacked${path.sep}dist${path.sep}log-worker.js`),
    unpacked
  );
  assert.ok(!unpacked.includes(`${path.sep}app.asar${path.sep}`));
  // A development path is returned untouched.
  assert.strictEqual(unpackedWorkerPath(`${path.sep}repo${path.sep}desktop${path.sep}dist${path.sep}log-worker.js`), `${path.sep}repo${path.sep}desktop${path.sep}dist${path.sep}log-worker.js`);
});
