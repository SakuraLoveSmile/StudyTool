import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { EventEmitter } from 'node:events';
import { BackendManager } from '../backend-manager';
import { CredentialsStore, SafeStorageInterface } from '../credentials-store';

const storage: SafeStorageInterface = {
  isEncryptionAvailable: () => true,
  encryptString: (value) => Buffer.from(value),
  decryptString: (value) => value.toString(),
};

class StubProcess extends EventEmitter {
  pid = 90001;
  exitCode: number | null = null;
  killed = false;
  stdin = { write: () => true };
  stdout = new EventEmitter();
  stderr = new EventEmitter();

  kill(): boolean {
    this.killed = true;
    this.exitCode = 0;
    this.emit('exit', 0, 'SIGKILL');
    return true;
  }
}

function manager(port: number, onEvent?: (event: string, fields: Record<string, unknown>) => void): BackendManager {
  return new BackendManager(
    port,
    '127.0.0.1',
    'startup-test-token',
    new CredentialsStore('/tmp/studytool-startup-test', storage),
    false,
    { onEvent: (event, _level, _message, fields = {}) => onEvent?.(event, fields) },
  );
}

async function listen(handler: http.RequestListener): Promise<http.Server> {
  const server = http.createServer(handler);
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', () => resolve()));
  return server;
}

function portOf(server: http.Server): number {
  return (server.address() as { port: number }).port;
}

test('packaged and development backends use their respective startup budgets', () => {
  const dev = manager(18764);
  const packaged = new BackendManager(
    18764,
    '127.0.0.1',
    'startup-test-token',
    new CredentialsStore('/tmp/studytool-packaged-startup-test', storage),
    true,
  );
  assert.equal((dev as any).getStartupBudgetMs(), 15_000);
  assert.equal((packaged as any).getStartupBudgetMs(), 90_000);
});

test('waitForReady accepts a delayed 200 response within its remaining budget', async () => {
  const server = await listen((_req, res) => {
    setTimeout(() => {
      res.writeHead(200);
      res.end('ok');
    }, 30);
  });
  try {
    const m = manager(portOf(server));
    (m as any).childProcess = new StubProcess();
    assert.equal(await m.waitForReady(250), true);
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
});

test('waitForReady treats non-200 and a hanging endpoint as not ready', async () => {
  let hanging = false;
  const server = await listen((_req, res) => {
    if (hanging) return;
    res.writeHead(503);
    res.end('starting');
  });
  try {
    const m = manager(portOf(server));
    (m as any).childProcess = new StubProcess();
    assert.equal(await m.waitForReady(80), false);
    hanging = true;
    assert.equal(await m.waitForReady(80), false);
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
});

test('waitForReady schedules one retry when a request times out and is destroyed', async () => {
  let requestCount = 0;
  const server = await listen(() => {
    requestCount += 1;
  });
  try {
    const m = manager(portOf(server));
    (m as any).childProcess = new StubProcess();
    assert.equal(await m.waitForReady(1600), false);
    assert.equal(requestCount, 2);
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
});

test('start reports a child that exits before readiness once', async () => {
  const events: string[] = [];
  const m = manager(1, (event) => events.push(event));
  (m as any).getBackendExecutable = () => ({ command: process.execPath, args: ['-e', 'process.exit(1)', '--'], cwd: process.cwd() });
  (m as any).getStartupBudgetMs = () => 250;
  assert.equal(await m.start(), false);
  assert.equal(events.filter((event) => event === 'startup.backend.exited_before_ready').length, 1);
  assert.equal(events.includes('startup.backend.ready_timeout'), false);
});

test('start uses the short development budget for a readiness timeout and confirms termination', async () => {
  const events: Array<{ event: string; fields: Record<string, unknown> }> = [];
  const server = await listen(() => undefined);
  const m = manager(portOf(server), (event, fields) => events.push({ event, fields }));
  (m as any).getBackendExecutable = () => ({ command: process.execPath, args: ['-e', 'setTimeout(() => {}, 10000)', '--'], cwd: process.cwd() });
  (m as any).getStartupBudgetMs = () => 100;
  try {
    assert.equal(await m.start(), false);
    const timeout = events.find(({ event }) => event === 'startup.backend.ready_timeout');
    assert.ok(timeout);
    assert.equal(timeout.fields.timeout_config, '100ms');
    assert.equal(timeout.fields.failure_reason, 'ready_timeout');
    assert.equal(timeout.fields.termination_confirmed, true);
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
});

test('tracked process readiness uses the same budget and reports failed termination confirmation', async () => {
  const events: Array<{ event: string; fields: Record<string, unknown> }> = [];
  const m = manager(1, (event, fields) => events.push({ event, fields }));
  const proc = new StubProcess();
  (m as any).childProcess = proc;
  (m as any).getStartupBudgetMs = () => 20;
  (m as any).waitForReady = async () => {
    await new Promise((resolve) => setTimeout(resolve, 30));
    return false;
  };
  (m as any).forceKill = async () => false;
  assert.equal(await m.start(), false);
  const timeout = events.find(({ event }) => event === 'startup.backend.ready_timeout');
  assert.ok(timeout);
  assert.equal(timeout.fields.timeout_config, '20ms');
  assert.equal(timeout.fields.termination_failed, true);
  assert.equal((m as any).childProcess, proc);
});

test('tracked process that exits during readiness emits exited_before_ready', async () => {
  const events: string[] = [];
  const m = manager(1, (event) => events.push(event));
  const proc = new StubProcess();
  (m as any).childProcess = proc;
  (m as any).getStartupBudgetMs = () => 100;
  (m as any).waitForReady = async () => {
    (m as any).childProcess = null;
    return false;
  };
  assert.equal(await m.start(), false);
  assert.equal(events.filter((event) => event === 'startup.backend.exited_before_ready').length, 1);
});

test('tracked process that exits after HTTP 200 is not reported as ready', async () => {
  const events: string[] = [];
  const m = manager(1, (event) => events.push(event));
  const proc = new StubProcess();
  (m as any).childProcess = proc;
  (m as any).getStartupBudgetMs = () => 100;
  (m as any).waitForReady = async () => {
    proc.exitCode = 1;
    return true;
  };
  assert.equal(await m.start(), false);
  assert.equal(events.includes('startup.backend.ready'), false);
  assert.equal(events.filter((event) => event === 'startup.backend.exited_before_ready').length, 1);
});
