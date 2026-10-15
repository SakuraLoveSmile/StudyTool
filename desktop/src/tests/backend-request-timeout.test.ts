/**
 * Tests for the local-backend request deadline used by `desktop:testConnection`.
 *
 * Two things must hold:
 *   1. the wait budget is 315 s for the OCR probe (PP-OCRv6 is an async jobs protocol)
 *      and stays at the existing 15 s for every other service;
 *   2. a reached deadline really terminates the in-flight HTTP request instead of leaving
 *      a hanging socket behind.
 */

import assert from 'node:assert/strict';
import fs from 'fs';
import http from 'node:http';
import path from 'node:path';
import { describe, it } from 'node:test';

import {
  BACKEND_REQUEST_TIMEOUT_CODE,
  DEFAULT_BACKEND_REQUEST_TIMEOUT_MS,
  OCR_BACKEND_REQUEST_TIMEOUT_MS,
  resolveConnectionTestTimeoutMs,
  terminateRequestOnTimeout,
} from '../connection-test';

interface LocalServer {
  port: number;
  /** Resolves once a client connection to this server has been closed. */
  connectionClosed: Promise<void>;
  close(): Promise<void>;
}

/** A server that accepts connections but never answers — the mid-probe OCR backend. */
function startHangingServer(): Promise<LocalServer> {
  return new Promise((resolve, reject) => {
    let markClosed: () => void = () => undefined;
    const connectionClosed = new Promise<void>((done) => {
      markClosed = done;
    });
    let alreadyClosed = false;

    const server = http.createServer(() => {
      /* deliberately silent: the client must give up on its own deadline */
    });
    server.on('connection', (socket) => {
      socket.on('close', () => {
        if (!alreadyClosed) {
          alreadyClosed = true;
          markClosed();
        }
      });
    });
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const address = server.address();
      const port = typeof address === 'object' && address ? address.port : 0;
      resolve({
        port,
        connectionClosed,
        close: () => new Promise<void>((done) => server.close(() => done())),
      });
    });
  });
}

/** Issues a request that carries the same deadline wiring the main process uses. */
function requestWithDeadline(
  server: { port: number },
  timeoutMs: number
): Promise<{ error?: Error & { code?: string }; status?: number }> {
  return new Promise((resolve, reject) => {
    const req = http.request(
      {
        host: '127.0.0.1',
        port: server.port,
        path: '/api/desktop/test-connection',
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        timeout: timeoutMs,
      },
      (res) => {
        res.resume();
        res.on('end', () => resolve({ status: res.statusCode }));
      }
    );
    terminateRequestOnTimeout(req, timeoutMs, '127.0.0.1:test-connection');
    req.on('error', (error) => resolve({ error }));
    req.on('close', () => reject(new Error('request closed without a response or an error')));
    req.end('{}');
  });
}

async function withDeadline<T>(promise: Promise<T>, ms: number, what: string): Promise<T> {
  let timer: NodeJS.Timeout;
  const timeout = new Promise<never>((_resolve, reject) => {
    timer = setTimeout(() => reject(new Error(`超时未完成: ${what}`)), ms);
  });
  try {
    return await Promise.race([promise, timeout]);
  } finally {
    clearTimeout(timer!);
  }
}

describe('resolveConnectionTestTimeoutMs', () => {
  it('gives the OCR connection test the 315 s jobs budget', () => {
    assert.equal(OCR_BACKEND_REQUEST_TIMEOUT_MS, 315_000);
    assert.equal(resolveConnectionTestTimeoutMs('baidu_ocr'), 315_000);
    assert.equal(resolveConnectionTestTimeoutMs('ocr'), 315_000);
    assert.equal(resolveConnectionTestTimeoutMs('  Baidu_OCR '), 315_000);
  });

  it('keeps every other service on the existing 15 s budget', () => {
    assert.equal(DEFAULT_BACKEND_REQUEST_TIMEOUT_MS, 15_000);
    for (const service of ['llm', 'embedding', 'unknown', '', '   ', null, undefined]) {
      assert.equal(
        resolveConnectionTestTimeoutMs(service as unknown as string),
        15_000,
        `service=${String(service)}`
      );
    }
  });
});

describe('terminateRequestOnTimeout', () => {
  it('destroys the request when the local backend never answers', async () => {
    const server = await startHangingServer();
    const startedAt = Date.now();
    try {
      const outcome = await withDeadline(requestWithDeadline(server, 60), 3000, '超时终止请求');

      const error = outcome.error as Error & { code?: string };
      assert.ok(error, '超时应以错误结束，而不是拿到响应');
      assert.equal(error.code, BACKEND_REQUEST_TIMEOUT_CODE);
      assert.equal(error.name, 'TimeoutError');
      assert.ok(Date.now() - startedAt < 3000, '超时后应立即结束，不应继续等到默认上限');

      // The socket is really gone: the server observes the teardown instead of holding
      // an abandoned connection open.
      await withDeadline(server.connectionClosed, 2000, '服务端观察到连接关闭');
    } finally {
      await server.close();
    }
  });

  it('leaves a prompt reply untouched', async () => {
    const server = http.createServer((_req, res) => {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: true }));
    });
    await new Promise<void>((done) => server.listen(0, '127.0.0.1', done));
    const address = server.address();
    const port = typeof address === 'object' && address ? address.port : 0;

    try {
      const outcome = await withDeadline(requestWithDeadline({ port }, 5000), 3000, '正常响应');
      assert.equal(outcome.status, 200);
      assert.equal(outcome.error, undefined);
    } finally {
      await new Promise<void>((done) => server.close(() => done()));
    }
  });
});

describe('desktop:testConnection wiring', () => {
  /**
   * `main.ts` cannot be imported here (it loads Electron at module scope), so the wiring is
   * asserted at the source level: the probe must resolve its budget through
   * `resolveConnectionTestTimeoutMs` and pass it to `makeBackendRequest`, and the request
   * builder must install the destroy-on-timeout handler.
   */
  it('resolves the OCR budget through the shared timeout helper', () => {
    const source = fs.readFileSync(path.join(__dirname, '..', '..', 'src', 'main.ts'), 'utf8');

    assert.match(source, /resolveConnectionTestTimeoutMs\(serviceType\)/);
    assert.match(source, /terminateRequestOnTimeout\(req, timeoutMs, target\)/);
    assert.match(source, /timeout: timeoutMs/);
    assert.match(source, /timeoutMs: number = DEFAULT_BACKEND_REQUEST_TIMEOUT_MS/);
    assert.doesNotMatch(source, /timeout:\s*15000\b/);
  });
});
