/**
 * Restricted feedback-log collector — worker side.
 *
 * Produces the payload for the "send diagnostics with feedback" flow: at most one
 * bounded JSONL excerpt per log source, covering only the last 30 minutes of the
 * CURRENT session. This is a narrower contract than the viewer query or the
 * diagnostic export, and it is deliberately re-enforced here rather than inherited:
 *
 *  - Second-pass whitelist + scrub. `queryLogs` already re-scrubs on read, but the
 *    reader normalizes legacy keys (`msg`, `asctime`, plain text) and attaches
 *    viewer-only fields (`record_id`), and a file written by an older build cannot
 *    be trusted to have gone through today's scrubber. So every emitted record is
 *    whitelisted and scrubbed AGAIN here; anything outside the schema is dropped
 *    and no malformed record ever falls back to its raw text.
 *  - Hard per-source budgets. Each source gets its own queryLogs call with an
 *    independent 1 MiB scan budget and a 500-entry page, so a huge desktop log can
 *    never starve the backend/frontend streams (≤ 3 MiB total).
 *  - Hard per-file size cap. Each emitted file is ≤ 256 KiB of UTF-8 including the
 *    metadata line. Overflow drops the OLDEST whole lines — a byte slice is never
 *    used, so a JSON line or a multi-byte character can never be cut in half.
 *
 * Field whitelist (authoritative source: FIELD_WHITELIST in diagnostics.ts, which
 * backend/app/diagnostics.py LOG_FIELD_WHITELIST mirrors, plus the reader-derived
 * `timestamp_ms` cursor). Enumerated for the contract: timestamp, timestamp_ms,
 * level, source, event, message, session_id, instance_id, app_version, platform,
 * arch, logger, phase, pid, error_type, error_name, error_message, stack,
 * stack_frames, frames, cause_chain, exception_type, failed_stage, stage, reason,
 * detail, component, info, reason_code, file, line, func, task_id, task_type,
 * unit_id, unit_index, scope, call_id, attempt, service, model, endpoint, method,
 * status_code, provider_request_id, duration_ms, timeout_config, retry_reason,
 * retry_wait_seconds, input_chars, input_bytes, result_count, exit_code, signal,
 * code, killed, code_name, occurrences, first_seen, last_seen, dropped, limit,
 * window_ms, count, total, ok, name, id, version, bytes, entries, truncated, note,
 * action, operation, route, target, kind, tags, has_more, page, page_size,
 * sources, levels.
 *
 * Note `request_id` is deliberately NOT a logged field (it only exists in IPC
 * envelopes and worker report fields, which scrubRecord drops at write time); the
 * persisted correlation fields are task_id / call_id / provider_request_id.
 * `record_id` is viewer-only identity and must never leave the process.
 *
 * Everything here runs inside the log worker thread — the main process only
 * schedules the job and forwards the result.
 */

import { queryLogs, ALL_SOURCES, type LogEntry } from './log-store';
import { scrubRecord, FIELD_WHITELIST, type LogSource } from './diagnostics';

export type FeedbackLogSource = 'desktop' | 'backend' | 'frontend';
export type FeedbackLogFilename =
  | 'studytool-desktop.jsonl'
  | 'studytool-backend.jsonl'
  | 'studytool-frontend.jsonl';

/** Job payload handed to the worker by the main process. `now` freezes the window. */
export interface FeedbackJobPayload {
  /** Log directory (main process `currentLogDir()`). Never echoed into the payload. */
  dir: string;
  /** This launch's SESSION_ID — records must match it exactly. */
  sessionId: string;
  /** app.getVersion(), captured in the per-file metadata line. */
  appVersion: string;
  /** Date.now() when the main process accepted the request. */
  now: number;
}

export interface FeedbackLogFile {
  filename: FeedbackLogFilename;
  text: string;
}

/** The `ok:true` variant of the frozen FeedbackLogsResult contract. */
export interface FeedbackLogsSuccess {
  ok: true;
  files: FeedbackLogFile[];
  truncated: boolean;
  unavailable: FeedbackLogSource[];
  skippedLines: number;
}

/** Codes this module may raise; the worker maps anything else to LOG_FEEDBACK_FAILED. */
export class FeedbackCollectionError extends Error {
  public readonly code: 'LOG_FEEDBACK_UNAVAILABLE' | 'LOG_FEEDBACK_FAILED';

  constructor(code: 'LOG_FEEDBACK_UNAVAILABLE' | 'LOG_FEEDBACK_FAILED', message: string) {
    super(message);
    this.name = 'FeedbackCollectionError';
    this.code = code;
  }
}

/** Fixed collection window: the 30 minutes before the request was accepted. */
const FEEDBACK_WINDOW_MS = 30 * 60 * 1000;
/** Per-source page cap — a source with more matches is reported truncated. */
const FEEDBACK_PAGE_SIZE = 500;
/**
 * Per-source raw scan budget. Each source is queried separately, so this bounds
 * every stream independently (≤ 3 MiB total) and one giant file cannot consume
 * the whole allowance.
 */
const FEEDBACK_SOURCE_SCAN_BUDGET = 1 * 1024 * 1024;
/** Hard cap per emitted file, metadata line included (UTF-8 bytes). */
export const FEEDBACK_FILE_MAX_BYTES = 256 * 1024;

/**
 * The second-pass whitelist: the shared diagnostics schema plus `timestamp_ms`,
 * which the reader derives for sorting (it is a number, never user content).
 */
const FEEDBACK_FIELD_WHITELIST: ReadonlySet<string> = new Set([...FIELD_WHITELIST, 'timestamp_ms']);

const FILENAME_BY_SOURCE: Record<FeedbackLogSource, FeedbackLogFilename> = {
  desktop: 'studytool-desktop.jsonl',
  backend: 'studytool-backend.jsonl',
  frontend: 'studytool-frontend.jsonl',
};

function isFeedbackSource(source: LogSource): source is FeedbackLogSource {
  return source === 'desktop' || source === 'backend' || source === 'frontend';
}

/**
 * Re-whitelists and re-scrubs one parsed entry for upload.
 *
 * `scrubRecord` applies the write-time schema (FIELD_WHITELIST) plus scrubString to
 * every string recursively — this is what catches legacy keys the reader copied
 * verbatim (`msg` → `message` is NOT scrubbed by parseLogLine) and anything an
 * older build persisted. The explicit FEEDBACK_FIELD_WHITELIST pass afterwards is
 * defense in depth for reader-added fields (`record_id` is already dropped by
 * scrubRecord; the check makes the guarantee independent of implementation details).
 *
 * `source` is forced to the file the record was actually read from, so a crafted
 * line inside desktop.jsonl cannot masquerade as another stream's record.
 */
function sanitizeEntry(entry: LogEntry, source: FeedbackLogSource): Record<string, unknown> {
  const scrubbed = scrubRecord(entry);
  for (const key of Object.keys(scrubbed)) {
    if (!FEEDBACK_FIELD_WHITELIST.has(key)) delete scrubbed[key];
  }
  scrubbed.timestamp_ms =
    typeof entry.timestamp_ms === 'number' && Number.isFinite(entry.timestamp_ms)
      ? Math.round(entry.timestamp_ms)
      : 0;
  scrubbed.source = source;
  if (typeof scrubbed.timestamp !== 'string' || !scrubbed.timestamp) {
    scrubbed.timestamp = scrubbed.timestamp_ms
      ? new Date(scrubbed.timestamp_ms as number).toISOString()
      : '';
  }
  if (typeof scrubbed.level !== 'string' || !scrubbed.level) scrubbed.level = 'INFO';
  if (typeof scrubbed.event !== 'string' || !scrubbed.event) scrubbed.event = 'log';
  if (typeof scrubbed.message !== 'string') scrubbed.message = String(scrubbed.message ?? '');
  return scrubbed;
}

interface SerializedLine {
  text: string;
  bytes: number;
}

interface BuiltFile {
  text: string;
  kept: number;
  dropped: number;
}

/**
 * Joins the metadata line plus as many NEWEST whole lines as fit under the cap.
 *
 * `lines` is sorted oldest→newest; overflow removes from the front so only
 * complete JSON lines survive — never a byte prefix. `records`/`truncated` inside
 * the metadata depend on the trim, so the loop recomputes the metadata line until
 * it is stable (it terminates: `kept` only decreases).
 */
function buildFileText(
  metaBase: Record<string, unknown>,
  lines: SerializedLine[],
  forcedTruncated: boolean
): BuiltFile | null {
  let kept = lines.length;
  let bodyBytes = 0;
  for (const line of lines) bodyBytes += line.bytes;

  for (;;) {
    const metaLine = `${JSON.stringify({
      ...metaBase,
      records: kept,
      truncated: forcedTruncated || kept < lines.length,
    })}\n`;
    const metaBytes = Buffer.byteLength(metaLine, 'utf8');
    let dropped = false;
    while (kept > 0 && metaBytes + bodyBytes > FEEDBACK_FILE_MAX_BYTES) {
      kept -= 1;
      bodyBytes -= lines[lines.length - kept - 1].bytes;
      dropped = true;
    }
    if (!dropped) {
      if (kept === 0) return null;
      const tail = lines
        .slice(lines.length - kept)
        .map((line) => line.text)
        .join('');
      return { text: metaLine + tail, kept, dropped: lines.length - kept };
    }
  }
}

/**
 * Collects the bounded, re-scrubbed feedback payload.
 *
 * Success returns the `ok:true` contract variant. Failure throws
 * FeedbackCollectionError: LOG_FEEDBACK_UNAVAILABLE when nothing readable exists
 * at all, LOG_FEEDBACK_FAILED for anything unexpected (the worker also scrubs the
 * message so no raw system path can leak through it).
 */
export function collectFeedbackLogs(payload: FeedbackJobPayload): FeedbackLogsSuccess {
  if (
    !payload ||
    typeof payload !== 'object' ||
    typeof payload.dir !== 'string' ||
    !payload.dir ||
    typeof payload.sessionId !== 'string' ||
    !payload.sessionId ||
    typeof payload.appVersion !== 'string' ||
    !Number.isFinite(payload.now)
  ) {
    throw new FeedbackCollectionError('LOG_FEEDBACK_FAILED', '反馈日志采集参数无效');
  }

  const to = Math.floor(payload.now);
  const from = to - FEEDBACK_WINDOW_MS;

  const unavailable: FeedbackLogSource[] = [];
  const perSource = new Map<FeedbackLogSource, LogEntry[]>();
  const perSourceSkipped = new Map<FeedbackLogSource, number>();
  const perSourceTruncated = new Map<FeedbackLogSource, boolean>();
  let totalEntries = 0;
  let skippedLines = 0;
  let truncated = false;

  for (const source of ALL_SOURCES) {
    if (!isFeedbackSource(source)) continue;
    const result = queryLogs({
      dir: payload.dir,
      sources: [source],
      from,
      to,
      sessionId: payload.sessionId,
      page: 1,
      pageSize: FEEDBACK_PAGE_SIZE,
      maxScanBytes: FEEDBACK_SOURCE_SCAN_BUDGET,
    });
    skippedLines += result.skippedLines;
    perSourceSkipped.set(source, result.skippedLines);
    if (result.unavailable.includes(source)) unavailable.push(source);
    // Exceeding the scan budget or the 500-entry page both count as truncation.
    const sourceTruncated = result.scanTruncated || result.hasMore;
    perSourceTruncated.set(source, sourceTruncated);
    if (sourceTruncated) truncated = true;
    perSource.set(source, result.entries);
    totalEntries += result.entries.length;
  }

  // Zero valid records AND at least one unreadable source is a real failure, not
  // an empty success — the caller must not mistake it for "nothing to report".
  if (totalEntries === 0 && unavailable.length > 0) {
    throw new FeedbackCollectionError(
      'LOG_FEEDBACK_UNAVAILABLE',
      '日志文件缺失或暂时不可读取，无法采集反馈日志'
    );
  }

  const files: FeedbackLogFile[] = [];
  for (const source of ALL_SOURCES) {
    if (!isFeedbackSource(source)) continue;
    const entries = perSource.get(source) ?? [];
    // A source with no matching records produces NO file — not even a bare
    // metadata line — so "empty" can never be mistaken for content.
    if (entries.length === 0) continue;

    const cleaned = entries
      .map((entry) => sanitizeEntry(entry, source))
      .sort((a, b) => (a.timestamp_ms as number) - (b.timestamp_ms as number));

    const lines: SerializedLine[] = [];
    for (const record of cleaned) {
      try {
        const text = `${JSON.stringify(record)}\n`;
        lines.push({ text, bytes: Buffer.byteLength(text, 'utf8') });
      } catch {
        // A record that cannot be serialized is dropped, never uploaded raw.
        truncated = true;
      }
    }
    if (lines.length === 0) {
      truncated = true;
      continue;
    }

    const metaBase = {
      kind: 'feedback-log-collection',
      app_version: payload.appVersion,
      platform: process.platform,
      arch: process.arch,
      from: new Date(from).toISOString(),
      to: new Date(to).toISOString(),
      source,
      unavailable: [...unavailable],
      skipped_lines: perSourceSkipped.get(source) ?? 0,
    };
    const built = buildFileText(metaBase, lines, perSourceTruncated.get(source) === true);
    if (!built || built.dropped > 0) truncated = true;
    if (!built) continue;
    files.push({ filename: FILENAME_BY_SOURCE[source], text: built.text });
  }

  return { ok: true, files, truncated, unavailable, skippedLines };
}
