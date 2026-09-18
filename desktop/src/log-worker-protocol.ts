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
  | 'LOG_EXPORT_FAILED'
  | 'LOG_FEEDBACK_BUSY'
  | 'LOG_FEEDBACK_TIMEOUT'
  | 'LOG_FEEDBACK_CANCELED'
  | 'LOG_FEEDBACK_UNAVAILABLE'
  | 'LOG_FEEDBACK_FAILED';

export type LogJobKind = 'query' | 'export' | 'feedback';

/**
 * The only codes a feedback job may ever surface, per the frozen IPC contract.
 * Anything else a feedback worker produces (a missing code, a query/export code,
 * a crash) is normalized to LOG_FEEDBACK_FAILED by the scheduler so the renderer
 * contract cannot be widened by an internal accident.
 */
export const FEEDBACK_ERROR_CODES: ReadonlySet<string> = new Set([
  'LOG_FEEDBACK_BUSY',
  'LOG_FEEDBACK_TIMEOUT',
  'LOG_FEEDBACK_CANCELED',
  'LOG_FEEDBACK_UNAVAILABLE',
  'LOG_FEEDBACK_FAILED',
]);

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

/**
 * Codes the caller may safely retry without changing anything.
 *
 * Feedback: only BUSY and TIMEOUT are retryable. BUSY is pure contention (the
 * in-flight job finishes and the next request succeeds); TIMEOUT recycles the
 * worker, so a retry runs on a fresh thread. CANCELED is user/app intent —
 * retrying would undo it. FAILED is a defect or a pathological record — an
 * automatic retry would just hit it again. UNAVAILABLE is debatable, but we
 * treat it as non-retryable on purpose: it means the log files are missing or
 * unreadable *right now*, a state that does not heal within an immediate
 * retry window, so the UI should report it instead of spinning.
 */
export const RETRYABLE_CODES: ReadonlySet<string> = new Set([
  'LOG_QUERY_TIMEOUT',
  'LOG_QUERY_SUPERSEDED',
  'LOG_WORKER_CRASHED',
  'LOG_EXPORT_TIMEOUT',
  'LOG_EXPORT_BUSY',
  'LOG_FEEDBACK_BUSY',
  'LOG_FEEDBACK_TIMEOUT',
]);

export const LOG_QUERY_TIMEOUT_MS = 10_000;
export const LOG_EXPORT_TIMEOUT_MS = 60_000;
/** Feedback collection is a bounded three-source scan; it must answer fast or not at all. */
export const LOG_FEEDBACK_TIMEOUT_MS = 2_500;
