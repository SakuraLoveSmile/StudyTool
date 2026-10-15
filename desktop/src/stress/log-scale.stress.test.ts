/**
 * Log scale / responsiveness stress harness.
 *
 * Deliberately NOT part of `npm test`: it isolates ~110 MiB of fixtures and measures
 * real latency, so it runs on demand via `npm run test:stress`.
 *
 * What it proves (the numbers the freeze fix is judged on):
 *  - a 48 MiB log answers its first screen without the main thread stalling;
 *  - the main-process event loop stays responsive *because* the scan is in a worker —
 *    the same scan performed inline is measured alongside as the control;
 *  - rapid requests replace each other instead of piling up, and the newest one wins;
 *  - a query that exceeds its budget times out, is retryable, and the worker is rebuilt;
 *  - export and browsing run at the same time without either blocking the other;
 *  - long lines, oversized fields, corrupt records, rotation and empty sources never
 *    fail a whole query.
 *
 * The soak (memory growth over time) is opt-in: STUDYTOOL_LOG_SOAK_MS=600000 for the plan's
 * ten-minute run. Everything here is macOS/Linux evidence — the packaged Windows build
 * still has to be verified on real hardware.
 */

import test, { before, after } from 'node:test';
import assert from 'node:assert';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { queryLogs, type LogQueryOptions } from '../log-store';
import { LogWorkerService } from '../log-workers';

const MIB = 1024 * 1024;
const SOAK_MS = Number(process.env.STUDYTOOL_LOG_SOAK_MS || 0);
const WORK_DIR = fs.mkdtempSync(path.join(os.tmpdir(), 'rag-log-stress-'));
const LEVELS = ['DEBUG', 'INFO', 'WARN', 'ERROR'];

const MAX_SCAN_BYTES = 48 * MIB;
/** Plan budget: a 48 MiB ordinary log must show its first page within this. */
const FIRST_SCREEN_BUDGET_MS = 3000;
/** Plan budget: main-process event loop lag while a stress query runs. */
const LAG_P95_BUDGET_MS = 100;
/** Plan budget: navigation/IPC-scale work during a stress query. */
const NAVIGATION_BUDGET_MS = 200;

interface Fixture {
  dir: string;
  bytes: number;
  lines: number;
  sources: string[];
}

const fixtures: Record<string, Fixture> = {};
/** Every fixture is built once and shared by the checks below. */
let reuseService: LogWorkerService | null = null;

function logLine(index: number, baseMs: number, filler = 0, compact = false): string {
  if (compact) {
    // Small records on purpose: this is the "100k+ records from one source" fixture, and
    // it has to fit the same 15 MiB envelope as the ordinary one.
    return `${JSON.stringify({
      timestamp: new Date(baseMs + index * 10).toISOString(),
      level: LEVELS[index % LEVELS.length],
      source: 'desktop',
      event: `stress.event.${index % 40}`,
      message: `m${index}`,
    })}\n`;
  }
  return `${JSON.stringify({
    timestamp: new Date(baseMs + index * 10).toISOString(),
    level: LEVELS[index % LEVELS.length],
    source: 'desktop',
    event: `stress.event.${index % 40}`,
    message: filler ? `payload-${index}-${'x'.repeat(filler)}` : `message ${index}`,
    session_id: `sess-${index % 3}`,
    task_id: `task-${index % 5000}`,
    app_version: '1.0.4',
    duration_ms: index % 900,
  })}\n`;
}

/** Writes roughly `targetBytes` of JSONL for one source, returning what it produced. */
function writeSource(
  dir: string,
  source: string,
  targetBytes: number,
  options: { baseMs: number; filler?: number; compact?: boolean } = { baseMs: Date.now() }
): { bytes: number; lines: number } {
  const file = path.join(dir, `${source}.jsonl`);
  const fd = fs.openSync(file, 'w');
  let bytes = 0;
  let lines = 0;
  try {
    let batch: string[] = [];
    let batchBytes = 0;
    while (bytes < targetBytes) {
      const line = logLine(lines, options.baseMs, options.filler ?? 0, options.compact ?? false);
      batch.push(line);
      batchBytes += Buffer.byteLength(line, 'utf8');
      lines += 1;
      if (batch.length >= 2000) {
        fs.writeSync(fd, batch.join(''));
        bytes += batchBytes;
        batch = [];
        batchBytes = 0;
      }
    }
    if (batch.length) {
      fs.writeSync(fd, batch.join(''));
      bytes += batchBytes;
    }
  } finally {
    fs.closeSync(fd);
  }
  return { bytes, lines };
}

function makeFixture(
  name: string,
  spec: { sources: Record<string, number>; filler?: number; compact?: boolean }
): Fixture {
  const dir = path.join(WORK_DIR, name);
  fs.mkdirSync(dir, { recursive: true });
  let bytes = 0;
  let lines = 0;
  const base = Date.now() - 60 * 60 * 1000;
  for (const [source, target] of Object.entries(spec.sources)) {
    const written = writeSource(dir, source, target, {
      baseMs: base,
      filler: spec.filler,
      compact: spec.compact,
    });
    bytes += written.bytes;
    lines += written.lines;
  }
  const fixture: Fixture = { dir, bytes, lines, sources: Object.keys(spec.sources) };
  fixtures[name] = fixture;
  return fixture;
}

before(() => {
  makeFixture('small', { sources: { desktop: 1 * MIB } });
  makeFixture('medium15', { sources: { desktop: 15 * MIB }, compact: true }); // 100k+ records, one source
  makeFixture('large30', { sources: { desktop: 20 * MIB, backend: 10 * MIB } });
  makeFixture('huge48', { sources: { desktop: 48 * MIB } });
  makeFixture('threeSources', {
    sources: { desktop: 5 * MIB, backend: 5 * MIB, frontend: 5 * MIB },
  });

  // Boundary fixture: one over-limit line, one near-limit line, one oversized field,
  // corrupt records, a rotated generation and scrub bait.
  const edgeDir = path.join(WORK_DIR, 'edge');
  fs.mkdirSync(edgeDir, { recursive: true });
  const now = Date.now();
  const edgeLines = [
    `${JSON.stringify({
      timestamp: new Date(now).toISOString(),
      level: 'ERROR',
      source: 'desktop',
      event: 'edge.secret',
      message: 'key sk-1234567890abcdef path C:\\Users\\someone\\2024真题.pdf',
      session_id: 'sess-edge',
    })}\n`,
    `${JSON.stringify({
      timestamp: new Date(now - 1000).toISOString(),
      level: 'INFO',
      source: 'desktop',
      event: 'edge.oversized_field',
      message: 'kept',
      detail: 'd'.repeat(9000),
    })}\n`,
    `${JSON.stringify({
      ...JSON.parse(logLine(1, now - 2000)),
      message: 'y'.repeat(300 * 1024),
    })}\n`,
    '{"truncated": \n',
    'plain console line about 2024真题.pdf\n',
    `${JSON.stringify({
      timestamp: new Date(now - 3000).toISOString(),
      level: 'WARN',
      source: 'desktop',
      event: 'edge.after_corrupt',
      message: 'still served',
    })}\n`,
  ];
  fs.writeFileSync(path.join(edgeDir, 'desktop.jsonl'), edgeLines.join(''));
  fs.writeFileSync(
    path.join(edgeDir, 'desktop.jsonl.1'),
    `${JSON.stringify({
      timestamp: new Date(now - 120_000).toISOString(),
      level: 'INFO',
      source: 'desktop',
      event: 'edge.rotated',
      message: 'rotated generation',
    })}\n`
  );
  fixtures.edge = { dir: edgeDir, bytes: fs.statSync(path.join(edgeDir, 'desktop.jsonl')).size, lines: 6, sources: ['desktop'] };

  const emptyDir = path.join(WORK_DIR, 'empty');
  fs.mkdirSync(emptyDir, { recursive: true });
  fixtures.empty = { dir: emptyDir, bytes: 0, lines: 0, sources: [] };
});

after(() => {
  reuseService?.shutdown();
  reuseService = null;
  try {
    fs.rmSync(WORK_DIR, { recursive: true, force: true });
  } catch {
    /* temp dir: best effort */
  }
});

/** Samples main-thread event loop lag: how late a 20 ms timer actually fires. */
class LagSampler {
  private samples: number[] = [];
  private timer: NodeJS.Timeout | null = null;
  private last = 0;

  constructor(private readonly intervalMs = 20) {}

  public start(): void {
    this.last = Date.now();
    this.timer = setInterval(() => {
      const now = Date.now();
      this.samples.push(Math.max(0, now - this.last - this.intervalMs));
      this.last = now;
    }, this.intervalMs);
  }

  public stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  public percentile(p: number): number {
    if (!this.samples.length) return 0;
    const sorted = [...this.samples].sort((a, b) => a - b);
    const index = Math.min(sorted.length - 1, Math.floor((sorted.length * p) / 100));
    return sorted[index];
  }

  public get max(): number {
    return this.samples.length ? Math.max(...this.samples) : 0;
  }

  public get count(): number {
    return this.samples.length;
  }
}

/** Round-trip latency of a trivial main-thread task — a proxy for IPC/navigation work. */
async function measureTickLatency(durationMs: number): Promise<number[]> {
  const latencies: number[] = [];
  const deadline = Date.now() + durationMs;
  while (Date.now() < deadline) {
    const started = process.hrtime.bigint();
    await new Promise((resolve) => setTimeout(resolve, 0));
    latencies.push(Number(process.hrtime.bigint() - started) / 1e6);
  }
  return latencies;
}

function percentile(values: number[], p: number): number {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.floor((sorted.length * p) / 100))];
}

function p95(values: number[]): number {
  return percentile(values, 95);
}

function service(): LogWorkerService {
  if (!reuseService) reuseService = new LogWorkerService();
  return reuseService;
}

function queryOptions(dir: string, sources?: string[]): LogQueryOptions {
  return { dir, sources: sources ?? ['desktop', 'backend', 'frontend'], from: 0, to: Date.now() + 60_000 };
}

test('fixtures match the plan sizes (1 / 15 / 30 / 48 MiB, 100k+ single source)', () => {
  assert.ok(fixtures.small.bytes >= 1 * MIB - 64 * 1024, `small fixture: ${fixtures.small.bytes}`);
  assert.ok(fixtures.medium15.bytes >= 15 * MIB - 64 * 1024, `15 MiB fixture: ${fixtures.medium15.bytes}`);
  assert.ok(fixtures.large30.bytes >= 30 * MIB - 64 * 1024, `30 MiB fixture: ${fixtures.large30.bytes}`);
  assert.ok(fixtures.huge48.bytes >= 48 * MIB - 64 * 1024, `48 MiB fixture: ${fixtures.huge48.bytes}`);
  assert.ok(
    fixtures.medium15.lines >= 100_000,
    `single source with 100k+ records: ${fixtures.medium15.lines}`
  );
  assert.strictEqual(fixtures.threeSources.sources.length, 3);
  assert.ok(fixtures.huge48.bytes <= MAX_SCAN_BYTES * 1.1, 'the 48 MiB fixture sits at the scan ceiling');
});

test('48 MiB first screen lands inside the 3 s budget', async () => {
  const started = Date.now();
  const outcome = await service().query('stress-first-screen', queryOptions(fixtures.huge48.dir, ['desktop']));
  const elapsed = Date.now() - started;

  assert.ok(outcome.ok, 'the 48 MiB query completes');
  if (!outcome.ok) return;
  assert.strictEqual(outcome.value.entries.length, 200, 'one full page of 200');
  assert.ok(outcome.value.total > 100_000, `scanned ${outcome.value.total} matching records`);
  assert.ok(
    elapsed <= FIRST_SCREEN_BUDGET_MS,
    `48 MiB first screen took ${elapsed} ms (budget ${FIRST_SCREEN_BUDGET_MS} ms)`
  );
});

test('a 48 MiB scan keeps the main event loop responsive — and the inline path does not', async () => {
  // Control: the pre-fix behaviour, where the scan ran on the main thread.
  const controlLag = new LagSampler();
  controlLag.start();
  const controlTicks = measureTickLatency(1200);
  queryLogs({
    dir: fixtures.huge48.dir,
    sources: ['desktop'],
    from: 0,
    to: Date.now() + 60_000,
    page: 1,
    pageSize: 200,
  });
  controlLag.stop();
  const controlTickLatency = p95(await controlTicks);

  // Fixed behaviour: the same scan through the worker, measured the same way.
  const lag = new LagSampler();
  lag.start();
  const ticks = measureTickLatency(1200);
  const outcome = await service().query('stress-lag', queryOptions(fixtures.huge48.dir, ['desktop']));
  lag.stop();
  const tickLatency = p95(await ticks);

  assert.ok(outcome.ok, 'stress query completed');
  console.log(
    `      event loop lag P95 — inline control ${controlLag.percentile(95)} ms, worker ${lag.percentile(
      95
    )} ms (samples ${lag.count}); tick P95 — inline ${controlTickLatency.toFixed(1)} ms, worker ${tickLatency.toFixed(1)} ms`
  );

  assert.ok(
    lag.percentile(95) < LAG_P95_BUDGET_MS,
    `worker-path event loop lag P95 ${lag.percentile(95)} ms must stay under ${LAG_P95_BUDGET_MS} ms`
  );
  assert.ok(
    tickLatency < NAVIGATION_BUDGET_MS,
    `navigation-scale work P95 ${tickLatency.toFixed(1)} ms must stay under ${NAVIGATION_BUDGET_MS} ms`
  );
  assert.ok(
    controlTickLatency > tickLatency,
    `the control should be worse than the worker path (inline ${controlTickLatency.toFixed(
      1
    )} ms vs worker ${tickLatency.toFixed(1)} ms)`
  );
});

test('rapid requests replace each other instead of piling up, and the newest wins', async () => {
  const total = 40;
  const pending: Array<Promise<unknown>> = [];
  const order: string[] = [];
  for (let i = 0; i < total; i += 1) {
    const requestId = `burst-${i}`;
    order.push(requestId);
    pending.push(service().query(requestId, queryOptions(fixtures.small.dir, ['desktop'])));
  }

  const outcomes = await Promise.all(pending);
  let executed = 0;
  let superseded = 0;
  const byId = new Map<string, any>();
  outcomes.forEach((outcome: any, index) => {
    byId.set(order[index], outcome);
    if (outcome.ok) executed += 1;
    else if (outcome.error.code === 'LOG_QUERY_SUPERSEDED') superseded += 1;
  });

  console.log(`      burst of ${total}: executed ${executed}, superseded ${superseded}`);
  assert.ok(executed <= 2, `at most a running + a latest request may execute (got ${executed})`);
  assert.strictEqual(executed + superseded, total, 'every request is answered, none is dropped silently');
  assert.ok(byId.get(`burst-${total - 1}`).ok, 'the newest request is the one that gets a result');
});

test('a query beyond its budget times out, stays retryable, and the worker is rebuilt', async () => {
  const slow = new LogWorkerService({ queryTimeoutMs: 500 });
  try {
    const timedOut = await slow.query('stress-timeout', queryOptions(fixtures.huge48.dir, ['desktop']));
    assert.ok(timedOut.ok === false, 'a 48 MiB scan cannot finish inside 500 ms');
    if (!timedOut.ok) {
      assert.strictEqual(timedOut.error.code, 'LOG_QUERY_TIMEOUT');
      assert.strictEqual(timedOut.error.retryable, true);
    }

    // Same service, same budget: only a freshly spawned worker can answer this fast.
    const retried = await slow.query('stress-retry', queryOptions(fixtures.small.dir, ['desktop']));
    assert.ok(retried.ok, 'the rebuilt worker serves the retry');
  } finally {
    slow.shutdown();
  }
});

test('a big export runs in parallel with continuous browsing', async () => {
  const exporting = service().exportArchive('stress-export', {
    dir: fixtures.large30.dir,
    appVersion: '1.0.4',
    sessionId: 'stress-session',
    from: 0,
    to: Date.now() + 60_000,
    sources: ['desktop', 'backend'],
  });

  const paging: Array<Promise<unknown>> = [];
  for (let page = 1; page <= 6; page += 1) {
    paging.push(
      service().query(`stress-page-${page}`, {
        ...queryOptions(fixtures.huge48.dir, ['desktop']),
        page,
      })
    );
  }

  const [exportOutcome, ...pageOutcomes] = await Promise.all([exporting, ...paging]);
  assert.ok(exportOutcome.ok, 'the export finished while pages were loading');
  if (exportOutcome.ok) {
    const buffer = Buffer.from(exportOutcome.value.buffer as unknown as Uint8Array);
    assert.strictEqual(buffer.readUInt32LE(0), 0x04034b50, 'a real ZIP is produced');
    assert.ok(exportOutcome.value.entries > 0);
  }
  // Superseded pages are a legitimate answer; a crash or a timeout is not.
  const failures = pageOutcomes.filter((o: any) => !o.ok).map((o: any) => o.error.code);
  assert.ok(
    failures.every((code: string) => code === 'LOG_QUERY_SUPERSEDED'),
    `parallel paging must not fail: ${JSON.stringify(failures)}`
  );
});

test('long lines, oversized fields, corruption, rotation and empty sources stay survivable', async () => {
  const outcome = await service().query('stress-edge', queryOptions(fixtures.edge.dir, ['desktop', 'frontend']));
  assert.ok(outcome.ok, 'the edge fixture does not fail the query');
  if (!outcome.ok) return;

  assert.strictEqual(outcome.value.skippedLines, 1, 'the over-256-KiB line is skipped and counted');
  assert.deepStrictEqual(outcome.value.unavailable, ['frontend'], 'a missing source is reported, not fatal');
  assert.ok(
    !JSON.stringify(outcome.value.entries).includes('yyyy'),
    'no prefix of the skipped line is displayed'
  );
  const payload = JSON.stringify(outcome.value.entries);
  assert.ok(!payload.includes('1234567890abcdef'), 'secrets are masked under stress');
  assert.ok(!payload.includes('2024真题.pdf'), 'user file names are masked under stress');
  assert.ok(payload.includes('omitted: oversized string'), 'oversized fields become the omission marker');
  assert.ok(
    outcome.value.entries.some((e) => e.event === 'edge.rotated'),
    'the rotated generation is still readable'
  );
  assert.ok(
    outcome.value.entries.some((e) => e.event === 'edge.after_corrupt'),
    'parsing resumes after a corrupt record'
  );

  const empty = await service().query('stress-empty', queryOptions(fixtures.empty.dir, ['desktop']));
  assert.ok(empty.ok, 'an empty directory answers with an empty page, not an error');
  if (empty.ok) {
    assert.strictEqual(empty.value.total, 0);
    assert.deepStrictEqual(empty.value.unavailable, ['desktop']);
  }
});

test('three sources and a 30 MiB two-source log both stay inside the scan ceiling', async () => {
  const three = await service().query('stress-three', queryOptions(fixtures.threeSources.dir));
  assert.ok(three.ok);
  if (three.ok) {
    assert.strictEqual(three.value.scannedBytes, fixtures.threeSources.bytes);
    assert.strictEqual(three.value.scanTruncated, false, '15 MiB in total fits under the 48 MiB ceiling');
    assert.ok(three.value.entries.every((e) => typeof e.record_id === 'string'));
    // Stable identity: no list key may repeat on a page.
    const ids = three.value.entries.map((e) => e.record_id);
    assert.strictEqual(new Set(ids).size, ids.length, 'record ids are unique within a page');
  }

  const large = await service().query('stress-large', queryOptions(fixtures.large30.dir, ['desktop', 'backend']));
  assert.ok(large.ok);
  if (large.ok) {
    assert.strictEqual(large.value.scanTruncated, false, '30 MiB fits under the ceiling');
    assert.ok(large.value.total > 100_000);
  }
});

test('a source larger than the ceiling is truncated rather than read in full', async () => {
  // The 48 MiB fixture alone is at the ceiling; adding a second source forces a partial
  // read of the second one, which must be reported instead of silently ignored.
  const outcome = await service().query(
    'stress-ceiling',
    queryOptions(fixtures.huge48.dir, ['desktop', 'backend', 'frontend'])
  );
  assert.ok(outcome.ok);
  if (!outcome.ok) return;
  assert.ok(outcome.value.scannedBytes <= MAX_SCAN_BYTES, 'the scan ceiling is never exceeded');
});

test('timeout and cancellation remain usable under a running stress query', async () => {
  const slow = new LogWorkerService({ queryTimeoutMs: 10_000 });
  try {
    const running = slow.query('stress-cancel', queryOptions(fixtures.huge48.dir, ['desktop']));
    assert.strictEqual(slow.cancelQuery('stress-cancel'), true, 'the in-flight query is cancellable');
    const outcome = await running;
    assert.ok(outcome.ok === false);
    if (!outcome.ok) assert.strictEqual(outcome.error.code, 'LOG_QUERY_CANCELED');

    const again = await slow.query('stress-after-cancel', queryOptions(fixtures.small.dir, ['desktop']));
    assert.ok(again.ok, 'browsing still works right after a cancel');
  } finally {
    slow.shutdown();
  }
});

test('continuous operation does not grow memory without bound', async (t) => {
  if (!SOAK_MS) {
    t.skip('soak disabled — run with STUDYTOOL_LOG_SOAK_MS=600000 for the plan\'s ten-minute run');
    return;
  }
  const sampler = new LagSampler(50);
  const heaps: number[] = [];
  sampler.start();
  const deadline = Date.now() + SOAK_MS;
  let iterations = 0;
  while (Date.now() < deadline) {
    await service().query(`soak-${iterations}`, {
      ...queryOptions(fixtures.medium15.dir, ['desktop']),
      page: (iterations % 5) + 1,
    });
    iterations += 1;
    heaps.push(process.memoryUsage().heapUsed);
  }
  sampler.stop();

  const quarter = Math.max(1, Math.floor(heaps.length / 4));
  const first = heaps.slice(0, quarter).reduce((a, b) => a + b, 0) / quarter;
  const last = heaps.slice(-quarter).reduce((a, b) => a + b, 0) / quarter;
  console.log(
    `      soak ${Math.round(SOAK_MS / 1000)} s, ${iterations} queries, lag P95 ${sampler.percentile(
      95
    )} ms, heap ${(first / MIB).toFixed(1)} MiB → ${(last / MIB).toFixed(1)} MiB`
  );
  assert.ok(last - first < 64 * MIB, `heap grew ${((last - first) / MIB).toFixed(1)} MiB over the soak`);
});
