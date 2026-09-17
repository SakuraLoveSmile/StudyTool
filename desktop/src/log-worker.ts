/**
 * Worker-thread entry point for every expensive diagnostics job.
 *
 * This is where log reading, parsing, scrubbing, sorting and archive building happen.
 * The main process only validates IPC parameters, schedules jobs and owns the native
 * save dialog — so a 48 MiB scan or a pathological record can no longer stall the
 * event loop that keeps the window responsive.
 *
 * One job runs at a time per worker instance; the parent runs one instance for queries
 * and a second one for exports, so an export never blocks log browsing.
 */

import { parentPort } from 'worker_threads';
import { queryLogs, type LogQueryOptions } from './log-store';
import { buildDiagnosticArchive, type DiagnosticArchiveOptions } from './diagnostic-export';
import type { LogWorkerRequest, LogWorkerResponse } from './log-worker-protocol';

export type QueryJobPayload = LogQueryOptions;
export type ExportJobPayload = DiagnosticArchiveOptions;

/** Runs one job synchronously — each is CPU/IO bound and already isolated in a thread. */
function runJob(request: LogWorkerRequest): unknown {
  if (request.kind === 'query') {
    return queryLogs(request.payload as QueryJobPayload);
  }
  if (request.kind === 'export') {
    // The archive is built here, in the worker: the parent thread only validates the
    // request and writes the finished bytes through the native save dialog.
    return buildDiagnosticArchive(request.payload as ExportJobPayload);
  }
  throw new Error(`未知的日志任务类型: ${String((request as { kind?: unknown }).kind)}`);
}

if (parentPort) {
  const port = parentPort;
  port.on('message', (request: LogWorkerRequest) => {
    let response: LogWorkerResponse;
    try {
      response = { status: 'result', requestId: request.requestId, value: runJob(request) };
    } catch (err: any) {
      response = {
        status: 'error',
        requestId: request.requestId,
        code: request.kind === 'export' ? 'LOG_EXPORT_FAILED' : 'LOG_QUERY_FAILED',
        message: err?.message || String(err),
      };
    }
    port.postMessage(response);
  });
}
