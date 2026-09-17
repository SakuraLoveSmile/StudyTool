/**
 * Message contract between the main process and the diagnostics worker thread.
 *
 * Kept in its own module so the main process can import the types without pulling in
 * the worker entry point (whose top level binds a message listener).
 */

/** Every failure the viewer/export surface can report, so the UI can react precisely. */
export type LogWorkerErrorCode =
  | 'LOG_QUERY_TIMEOUT'
  | 'LOG_QUERY_CANCELED'
  | 'LOG_QUERY_SUPERSEDED'
  | 'LOG_WORKER_CRASHED'
  | 'LOG_QUERY_FAILED'
  | 'LOG_EXPORT_TIMEOUT'
  | 'LOG_EXPORT_CANCELED'
  | 'LOG_EXPORT_BUSY'
  | 'LOG_EXPORT_FAILED';

export type LogJobKind = 'query' | 'export';

export interface LogWorkerRequest {
  kind: LogJobKind;
  requestId: string;
  payload: unknown;
}

export interface LogWorkerResult {
  status: 'result';
  requestId: string;
  value: unknown;
}

export interface LogWorkerError {
  status: 'error';
  requestId: string;
  code: LogWorkerErrorCode;
  message: string;
}

export type LogWorkerResponse = LogWorkerResult | LogWorkerError;

/** Codes the caller may safely retry without changing anything. */
export const RETRYABLE_CODES: ReadonlySet<string> = new Set([
  'LOG_QUERY_TIMEOUT',
  'LOG_QUERY_SUPERSEDED',
  'LOG_WORKER_CRASHED',
  'LOG_EXPORT_TIMEOUT',
  'LOG_EXPORT_BUSY',
]);

export const LOG_QUERY_TIMEOUT_MS = 10_000;
export const LOG_EXPORT_TIMEOUT_MS = 60_000;
