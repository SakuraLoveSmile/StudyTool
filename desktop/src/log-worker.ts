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
import { collectFeedbackLogs, type FeedbackJobPayload } from './feedback-logs';
import { scrubString } from './diagnostics';
import {
  FEEDBACK_ERROR_CODES,
  type LogJobKind,
  type LogWorkerErrorCode,
  type LogWorkerRequest,
  type LogWorkerResponse,
} from './log-worker-protocol';

export type QueryJobPayload = LogQueryOptions;
export type ExportJobPayload = DiagnosticArchiveOptions;
export type { FeedbackJobPayload } from './feedback-logs';

/**
 * Failure code per job kind. Feedback keeps any LOG_FEEDBACK_* code the collector
 * raised (e.g. LOG_FEEDBACK_UNAVAILABLE) and normalizes everything else to
 * LOG_FEEDBACK_FAILED — it must never borrow the query/export codes.
 */
function jobErrorCode(kind: LogJobKind, err: unknown): LogWorkerErrorCode {
  if (kind === 'export') return 'LOG_EXPORT_FAILED';
  if (kind === 'feedback') {
    const code = (err as { code?: unknown })?.code;
    return typeof code === 'string' && FEEDBACK_ERROR_CODES.has(code)
      ? (code as LogWorkerErrorCode)
      : 'LOG_FEEDBACK_FAILED';
  }
  return 'LOG_QUERY_FAILED';
}

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
  if (request.kind === 'feedback') {
    // Restricted feedback collection: read, re-whitelist, re-scrub, bound and sort
    // all happen here; the main thread only schedules and forwards the result.
    return collectFeedbackLogs(request.payload as FeedbackJobPayload);
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
        code: jobErrorCode(request.kind, err),
        // Feedback failures must never carry a raw system path to the renderer —
        // scrubString collapses home/absolute paths to `<path>`.
        message:
          request.kind === 'feedback'
            ? scrubString(err?.message || String(err))
            : err?.message || String(err),
      };
    }
    port.postMessage(response);
  });
}
