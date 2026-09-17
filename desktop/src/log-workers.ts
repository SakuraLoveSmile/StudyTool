/**
 * Main-process scheduler for the diagnostics worker threads.
 *
 * The main process must stay free to service IPC and keep the window responsive, so
 * every expensive job (log scan, scrub, sort, archive build) is delegated to a worker
 * thread and this module only does bookkeeping: at most one running job per channel,
 * at most one *latest* waiting job, hard timeouts, cancellation, and worker recycling
 * after a timeout or a crash.
 *
 * Two independent channels are used on purpose: an export must never queue behind log
 * browsing, and a stuck query must never take the export down with it. Nodes without a
 * usable Worker implementation are irrelevant here — the app ships with Electron's
 * bundled Node, and the worker file is unpacked from app.asar so it is a real file.
 */

import path from 'path';
import { Worker } from 'worker_threads';
import type { LogQueryOptions, LogQueryResult } from './log-store';
import type { DiagnosticArchive, DiagnosticArchiveOptions } from './diagnostic-export';
import {
  LOG_EXPORT_TIMEOUT_MS,
  LOG_QUERY_TIMEOUT_MS,
  RETRYABLE_CODES,
  type LogJobKind,
  type LogWorkerErrorCode,
  type LogWorkerRequest,
} from './log-worker-protocol';

export interface LogJobError {
  code: LogWorkerErrorCode;
  message: string;
  retryable: boolean;
  elapsedMs: number;
}

export type JobOutcome<T> = { ok: true; value: T; elapsedMs: number } | { ok: false; error: LogJobError };

export interface LogWorkerServiceOptions {
  /** Overridable for tests; defaults to the compiled sibling of this module. */
  workerPath?: string;
  queryTimeoutMs?: number;
  exportTimeoutMs?: number;
  /** Structured logging hook (the main process routes this into the desktop log). */
  report?: (event: string, message: string, fields?: Record<string, unknown>) => void;
}

/**
 * Rewrites a compiled module path that lives inside `app.asar` to its unpacked twin.
 *
 * A packaged build keeps application code inside `app.asar`, but a worker thread needs
 * a real file on disk — so electron-builder unpacks exactly this module (see
 * `asarUnpack` in desktop/electron-builder.json5) and we point at the unpacked copy.
 * Getting this wrong is invisible until a packaged build runs it, so the rewrite is a
 * pure function with its own test rather than an inline string operation.
 */
export function unpackedWorkerPath(compiledPath: string): string {
  const packed = `${path.sep}app.asar${path.sep}`;
  const unpacked = `${path.sep}app.asar.unpacked${path.sep}`;
  return compiledPath.includes(packed) ? compiledPath.replace(packed, unpacked) : compiledPath;
}

/** Resolves the worker entry point next to the compiled main process. */
export function defaultWorkerPath(): string {
  return unpackedWorkerPath(path.join(__dirname, 'log-worker.js'));
}

interface PendingJob {
  requestId: string;
  payload: unknown;
  enqueuedAt: number;
  resolve: (outcome: JobOutcome<unknown>) => void;
}

interface ActiveJob extends PendingJob {
  startedAt: number;
  worker: Worker;
  timer: NodeJS.Timeout;
}

interface ChannelOptions {
  kind: LogJobKind;
  workerPath: string;
  timeoutMs: number;
  /** Code reported when the caller cancels the job. */
  cancelCode: LogWorkerErrorCode;
  cancelMessage: string;
  /** Code reported when a newer request replaces a still-waiting one. */
  supersedeCode: LogWorkerErrorCode;
  supersedeMessage: string;
  /**
   * When true a new request is refused while anything is running or waiting.
   * Exports use this: silently running a queued second export would write the file
   * twice for one user action. Queries instead keep the newest one and drop the rest.
   */
  rejectWhenBusy?: boolean;
  timeoutCode: LogWorkerErrorCode;
  timeoutMessage: string;
  report?: (event: string, message: string, fields?: Record<string, unknown>) => void;
}

/**
 * One worker, one running job, one waiting slot.
 *
 * Cancellation and timeout both recycle the thread instead of asking it to stop: the
 * job body is synchronous, so a worker that is inside a scan cannot observe a message
 * anyway. Killing it is the only way to actually stop the work.
 */
class WorkerChannel {
  private worker: Worker | null = null;
  private active: ActiveJob | null = null;
  private pending: PendingJob | null = null;
  private stopping = false;

  constructor(private readonly options: ChannelOptions) {}

  public submit(requestId: string, payload: unknown): Promise<JobOutcome<unknown>> {
    return new Promise((resolve) => {
      if (this.stopping) {
        resolve(this.failure(this.options.cancelCode, '应用正在退出，任务未执行', Date.now()));
        return;
      }
      if (this.options.rejectWhenBusy && (this.active || this.pending)) {
        resolve(this.failure(this.options.supersedeCode, this.options.supersedeMessage, Date.now()));
        return;
      }
      const job: PendingJob = { requestId, payload, enqueuedAt: Date.now(), resolve };

      // Only the newest request is remembered: a backlog of stale queries would all be
      // answered with data the caller has already moved past.
      if (this.pending) {
        const replaced = this.pending;
        this.pending = null;
        replaced.resolve(this.failure(this.options.supersedeCode, this.options.supersedeMessage, replaced.enqueuedAt));
      }

      if (this.active) {
        this.pending = job;
        return;
      }
      this.start(job);
    });
  }

  public cancel(requestId: string): boolean {
    if (this.active && this.active.requestId === requestId) {
      const job = this.active;
      this.active = null;
      clearTimeout(job.timer);
      this.killWorker();
      job.resolve(this.failure(this.options.cancelCode, this.options.cancelMessage, job.startedAt));
      this.pump();
      return true;
    }
    if (this.pending && this.pending.requestId === requestId) {
      const job = this.pending;
      this.pending = null;
      job.resolve(this.failure(this.options.cancelCode, this.options.cancelMessage, job.enqueuedAt));
      return true;
    }
    return false;
  }

  public shutdown(): void {
    this.stopping = true;
    const active = this.active;
    this.active = null;
    if (active) {
      clearTimeout(active.timer);
      active.resolve(this.failure(this.options.cancelCode, '应用正在退出，任务已取消', active.startedAt));
    }
    const pending = this.pending;
    this.pending = null;
    if (pending) {
      pending.resolve(this.failure(this.options.cancelCode, '应用正在退出，任务已取消', pending.enqueuedAt));
    }
    this.killWorker();
  }

  private failure(code: LogWorkerErrorCode, message: string, since: number): JobOutcome<unknown> {
    return {
      ok: false,
      error: {
        code,
        message,
        retryable: RETRYABLE_CODES.has(code),
        elapsedMs: Math.max(0, Date.now() - since),
      },
    };
  }

  private ensureWorker(): Worker | null {
    if (this.stopping) return null;
    if (this.worker) return this.worker;
    let worker: Worker;
    try {
      worker = new Worker(this.options.workerPath);
    } catch (err: any) {
      this.options.report?.('diagnostics.worker.spawn_failed', '日志 Worker 启动失败', {
        kind: this.options.kind,
        error_message: err?.message,
      });
      return null;
    }
    worker.on('message', (message: unknown) => this.onMessage(worker, message));
    worker.on('error', (err: Error) => this.onWorkerFailure(worker, err?.message || '日志 Worker 异常'));
    worker.on('exit', (code: number) => {
      // A worker we are still tracking leaving on its own is a crash from here on,
      // even when it reports a clean exit code.
      if (this.worker === worker && !this.stopping) {
        this.onWorkerFailure(worker, `日志 Worker 意外退出（code=${code}）`);
      } else if (this.worker === worker) {
        this.worker = null;
      }
    });
    this.worker = worker;
    return worker;
  }

  private start(job: PendingJob): void {
    const worker = this.ensureWorker();
    if (!worker) {
      job.resolve(this.failure('LOG_WORKER_CRASHED', '日志 Worker 不可用，请重试', job.enqueuedAt));
      return;
    }
    const active = {
      ...job,
      startedAt: Date.now(),
      worker,
      timer: undefined as unknown as NodeJS.Timeout,
    } as ActiveJob;
    const timer = setTimeout(() => this.onTimeout(active), this.options.timeoutMs);
    // Never hold the process open just for a pending timeout.
    if (typeof timer.unref === 'function') timer.unref();
    active.timer = timer;
    this.active = active;

    const request: LogWorkerRequest = {
      kind: this.options.kind,
      requestId: job.requestId,
      payload: job.payload,
    };
    try {
      worker.postMessage(request);
    } catch (err: any) {
      this.active = null;
      clearTimeout(timer);
      job.resolve(this.failure('LOG_WORKER_CRASHED', err?.message || '日志任务下发失败', job.enqueuedAt));
    }
  }

  private onMessage(worker: Worker, message: unknown): void {
    const active = this.active;
    if (!active || active.worker !== worker) return;
    const response = message as { status?: string; requestId?: string; value?: unknown; code?: string; message?: string };
    if (!response || response.requestId !== active.requestId) return;
    this.active = null;
    clearTimeout(active.timer);
    if (response.status === 'error') {
      active.resolve(
        this.failure((response.code as LogWorkerErrorCode) || 'LOG_QUERY_FAILED', response.message || '日志任务失败', active.startedAt)
      );
    } else {
      active.resolve({ ok: true, value: response.value, elapsedMs: Math.max(0, Date.now() - active.startedAt) });
    }
    this.pump();
  }

  private onTimeout(job: ActiveJob): void {
    if (this.active !== job) return;
    this.active = null;
    this.killWorker();
    this.options.report?.('diagnostics.worker.timeout', '日志任务超时，已重建 Worker', {
      kind: this.options.kind,
      request_id: job.requestId,
      duration_ms: Date.now() - job.startedAt,
      timeout_config: this.options.timeoutMs,
    });
    job.resolve(this.failure(this.options.timeoutCode, this.options.timeoutMessage, job.startedAt));
    this.pump();
  }

  private onWorkerFailure(worker: Worker, message: string): void {
    if (this.worker === worker) this.worker = null;
    try {
      void worker.terminate();
    } catch {
      /* already gone */
    }
    const active = this.active && this.active.worker === worker ? this.active : null;
    if (active) {
      this.active = null;
      clearTimeout(active.timer);
      this.options.report?.('diagnostics.worker.crashed', '日志 Worker 崩溃', {
        kind: this.options.kind,
        request_id: active.requestId,
        error_message: message,
      });
      active.resolve(this.failure('LOG_WORKER_CRASHED', message, active.startedAt));
    }
    this.pump();
  }

  private pump(): void {
    if (this.stopping || this.active || !this.pending) return;
    const job = this.pending;
    this.pending = null;
    this.start(job);
  }

  private killWorker(): void {
    const worker = this.worker;
    this.worker = null;
    if (!worker) return;
    try {
      void worker.terminate();
    } catch {
      /* already gone */
    }
  }
}

/**
 * Public surface used by `main.ts`. Both channels share the same worker entry file but
 * run as separate threads with separate timeouts and separate failure domains.
 */
export class LogWorkerService {
  private readonly queryChannel: WorkerChannel;
  private readonly exportChannel: WorkerChannel;

  constructor(options: LogWorkerServiceOptions = {}) {
    const workerPath = options.workerPath ?? defaultWorkerPath();
    const report = options.report;
    this.queryChannel = new WorkerChannel({
      kind: 'query',
      workerPath,
      timeoutMs: options.queryTimeoutMs ?? LOG_QUERY_TIMEOUT_MS,
      cancelCode: 'LOG_QUERY_CANCELED',
      cancelMessage: '查询已取消',
      supersedeCode: 'LOG_QUERY_SUPERSEDED',
      supersedeMessage: '已有更新的查询在处理，本次请求已被取代',
      timeoutCode: 'LOG_QUERY_TIMEOUT',
      timeoutMessage: `日志查询超过 ${Math.round((options.queryTimeoutMs ?? LOG_QUERY_TIMEOUT_MS) / 1000)} 秒未完成，已中止，可重试`,
      report,
    });
    this.exportChannel = new WorkerChannel({
      kind: 'export',
      workerPath,
      timeoutMs: options.exportTimeoutMs ?? LOG_EXPORT_TIMEOUT_MS,
      cancelCode: 'LOG_EXPORT_CANCELED',
      cancelMessage: '导出已取消',
      supersedeCode: 'LOG_EXPORT_BUSY',
      supersedeMessage: '已有导出任务在进行中，请等待其完成',
      rejectWhenBusy: true,
      timeoutCode: 'LOG_EXPORT_TIMEOUT',
      timeoutMessage: `诊断包导出超过 ${Math.round((options.exportTimeoutMs ?? LOG_EXPORT_TIMEOUT_MS) / 1000)} 秒未完成，已中止，可重试`,
      report,
    });
  }

  public query(requestId: string, options: LogQueryOptions): Promise<JobOutcome<LogQueryResult>> {
    return this.queryChannel.submit(requestId, options) as Promise<JobOutcome<LogQueryResult>>;
  }

  public cancelQuery(requestId: string): boolean {
    return this.queryChannel.cancel(requestId);
  }

  public exportArchive(
    requestId: string,
    options: DiagnosticArchiveOptions
  ): Promise<JobOutcome<DiagnosticArchive>> {
    return this.exportChannel.submit(requestId, options) as Promise<JobOutcome<DiagnosticArchive>>;
  }

  public shutdown(): void {
    this.queryChannel.shutdown();
    this.exportChannel.shutdown();
  }
}
