/**
 * Log reader / normalizer for the desktop process.
 *
 * One reader serves both the in-app viewer (IPC query) and the diagnostic export (ZIP),
 * so "what you see" and "what you send" can never diverge.
 *
 * Three gates live here:
 *  - legacy compatibility: old records (no source / asctime / non-JSON lines) are
 *    normalized instead of being dropped;
 *  - export re-scrubbing: every entry is passed through the whitelist + scrubber again
 *    on read, so a file written by an older build cannot leak through the export path;
 *  - bounded work: files are streamed in fixed-size chunks and pathological lines are
 *    skipped whole, so a 48 MiB scan cannot blow up the call stack, the heap, or the
 *    regex engine. Each of those three was a real freeze in this reader.
 */

import fs from 'fs';
import path from 'path';
import { StringDecoder } from 'string_decoder';
import { LOG_BACKUP_COUNT, scrubRecord, scrubString, logFilePath, type LogSource } from './diagnostics';

export const ALL_SOURCES: LogSource[] = ['desktop', 'backend', 'frontend'];
export const DEFAULT_PAGE_SIZE = 200;
/** Never read more than this much raw text per query/export, to bound memory and latency. */
export const MAX_SCAN_BYTES = 48 * 1024 * 1024;
/** Export cap for raw log payload (release gate: 20 MiB, newest records win). */
export const EXPORT_MAX_RAW_BYTES = 20 * 1024 * 1024;
/** One read() never asks for more than this, so a 48 MiB scan never becomes one string. */
const READ_CHUNK_BYTES = 1024 * 1024;
/** Lines longer than this are skipped whole and counted — never rendered as a prefix. */
export const MAX_LINE_BYTES = 256 * 1024;
/**
 * Below this code-unit count a line cannot exceed MAX_LINE_BYTES: UTF-8 encodes at most
 * 3 bytes per UTF-16 code unit, so 256 KiB / 4 is a safe fast path and the exact byte
 * measurement only ever runs on genuinely large lines.
 */
const LINE_BYTES_PROBE_CHARS = Math.floor(MAX_LINE_BYTES / 4);

export interface LogEntry {
  timestamp: string;
  timestamp_ms: number;
  level: string;
  source: string;
  event: string;
  message: string;
  session_id: string | null;
  /** Viewer-only stable identity for list keys; never written to the export payload. */
  record_id?: string;
  [key: string]: unknown;
}

export interface LogQueryOptions {
  dir: string;
  sources?: string[];
  from?: number;
  to?: number;
  levels?: string[];
  sessionId?: string;
  taskId?: string;
  keyword?: string;
  page?: number;
  pageSize?: number;
  /**
   * Overrides MAX_SCAN_BYTES. Never set from IPC — the renderer cannot raise or lower
   * the scan ceiling — it exists so the truncation path can be exercised without
   * materialising a 48 MiB fixture on every test run.
   */
  maxScanBytes?: number;
}

export interface LogQueryResult {
  ok: boolean;
  entries: LogEntry[];
  /** Entries matching the filters *within this scan* — never a full-history count. */
  total: number;
  page: number;
  pageSize: number;
  hasMore: boolean;
  scannedBytes: number;
  scanTruncated: boolean;
  unavailable: string[];
  /** Lines dropped for exceeding MAX_LINE_BYTES; surfaced so the loss is never silent. */
  skippedLines: number;
  /**
   * Correlation id, echoed back to the renderer so it can cancel and can discard a
   * response that a newer request has already replaced. Set by the main process.
   */
  requestId?: string;
  /** Failure classification — present only when `ok` is false. */
  code?: string;
  message?: string;
  retryable?: boolean;
  elapsedMs?: number;
}

function normalizeLevel(raw: unknown): string {
  const value = String(raw ?? 'INFO').trim().toUpperCase();
  if (value === 'WARNING') return 'WARN';
  if (value === 'CRITICAL' || value === 'FATAL') return 'ERROR';
  if (value === 'TRACE' || value === 'NOTSET') return 'INFO';
  return value || 'INFO';
}

function toEpochMs(raw: unknown): number {
  if (typeof raw === 'number' && Number.isFinite(raw)) {
    // Tolerate seconds-precision legacy timestamps.
    return raw < 1e12 ? Math.round(raw * 1000) : Math.round(raw);
  }
  const text = String(raw ?? '').trim();
  if (!text) return 0;
  const parsed = Date.parse(text.includes('T') || text.includes('Z') ? text : text.replace(' ', 'T'));
  if (Number.isFinite(parsed)) return parsed;
  const numeric = Number(text);
  if (Number.isFinite(numeric)) return numeric < 1e12 ? Math.round(numeric * 1000) : Math.round(numeric);
  return 0;
}

/**
 * Normalizes one raw line into the shared entry shape.
 * Accepts: current JSONL, older JSON keys (asctime/levelname/msg), and plain text lines.
 */
export function parseLogLine(line: string, fallbackSource: string): LogEntry | null {
  const text = line.trim();
  if (!text) return null;

  let parsed: Record<string, unknown> | null = null;
  if (text.startsWith('{')) {
    try {
      const candidate = JSON.parse(text);
      if (candidate && typeof candidate === 'object' && !Array.isArray(candidate)) {
        parsed = candidate as Record<string, unknown>;
      }
    } catch {
      parsed = null;
    }
  }

  if (!parsed) {
    // Legacy plain-text console line: keep it as an informational record, scrubbed.
    return {
      timestamp: new Date(0).toISOString(),
      timestamp_ms: 0,
      level: 'INFO',
      source: fallbackSource,
      event: 'legacy.text',
      message: scrubString(text).slice(0, 500),
      session_id: null,
    };
  }

  const scrubbed = scrubRecord(parsed);
  const ms = toEpochMs(parsed.timestamp ?? parsed.asctime ?? parsed.time ?? parsed.ts);
  const entry: LogEntry = {
    ...scrubbed,
    timestamp: ms ? new Date(ms).toISOString() : String(parsed.timestamp ?? ''),
    timestamp_ms: ms,
    level: normalizeLevel(parsed.level ?? parsed.levelname),
    source: String(parsed.source ?? fallbackSource),
    event: String(scrubbed.event ?? parsed.msg_type ?? parsed.name ?? 'log'),
    message: String(scrubbed.message ?? parsed.msg ?? ''),
    session_id: parsed.session_id ? String(parsed.session_id) : null,
  };
  // The durable record id drives export de-duplication of pending-queue records.
  // FIELD_WHITELIST gains it in diagnostics.ts; preserving it here keeps the read
  // side correct regardless of landing order, and the id is an opaque
  // `session:source:seq` token that carries no user content.
  if (typeof parsed.log_record_id === 'string' && parsed.log_record_id) {
    entry.log_record_id = parsed.log_record_id;
  }
  return entry;
}

/**
 * One sanitized per-candidate read failure, safe to disclose in the export manifest:
 * relative file name (basename), stage and normalized code only — never an absolute
 * path, raw exception text or log-line content.
 */
export interface SourceReadError {
  file: string;
  stage: 'stat' | 'open' | 'read';
  code: string;
}

interface SourceReadResult {
  entries: LogEntry[];
  bytes: number;
  truncated: boolean;
  unavailable: boolean;
  /**
   * True only when every candidate we actually attempted was absent (ENOENT):
   * attempted>0, no non-ENOENT failure, nothing read. A budget-interrupted scan is
   * judged on the part it scanned — generations it never reached do not count.
   */
  missing: boolean;
  skippedLines: number;
  /** All per-candidate failures, sanitized for disclosure; `error` keeps only the first. */
  errors: SourceReadError[];
  error?: LogReadError;
}

interface LineStreamResult {
  bytesRead: number;
  skippedLines: number;
  error?: LogReadError;
  failed: boolean;
}

/** A sanitized, stage-aware filesystem failure safe to expose through IPC. */
export class LogReadError extends Error {
  public readonly source: string;
  public readonly stage: 'stat' | 'open' | 'read';
  public readonly code: string;

  constructor(source: string, stage: 'stat' | 'open' | 'read', error: unknown) {
    const code = readErrorCode(error);
    const detail = code === 'EACCES' || code === 'EPERM' ? `权限被拒绝（${code}）` : `读取失败（${code}）`;
    super(`日志读取失败：${source} ${detail}`);
    this.name = 'LogReadError';
    this.source = source;
    this.stage = stage;
    this.code = code;
  }
}

function readErrorCode(error: unknown): string {
  const code = (error as { code?: unknown })?.code;
  return typeof code === 'string' && /^[A-Z0-9_]+$/.test(code) ? code : 'UNKNOWN';
}

function isMissing(error: unknown): boolean {
  return readErrorCode(error) === 'ENOENT';
}

/**
 * Streams the byte range [start, size) of one file as whole lines.
 *
 * Chunked reads plus `StringDecoder` replace the old "read the whole range, then
 * `raw.split(/\r?\n/)`" approach: that allocated a second full-size string plus a
 * huge array of fragments, which is what made a large log fatal. Memory here is
 * bounded by READ_CHUNK_BYTES regardless of file size.
 *
 * A line longer than MAX_LINE_BYTES is discarded in place — nothing of it is ever
 * handed to the parser or the viewer — and counted exactly once.
 */
function streamLines(
  file: string,
  start: number,
  size: number,
  onLine: (line: string) => void,
  source: string
): LineStreamResult {
  const result: LineStreamResult = { bytesRead: 0, skippedLines: 0, failed: false };
  let fd: number;
  try {
    fd = fs.openSync(file, 'r');
  } catch (error) {
    if (!isMissing(error)) result.error = new LogReadError(source, 'open', error);
    result.failed = true;
    return result;
  }

  try {
    const decoder = new StringDecoder('utf8');
    const chunk = Buffer.allocUnsafe(Math.min(READ_CHUNK_BYTES, Math.max(1, size - start)));
    // When we only take the tail, the first fragment is half a line: drop it.
    let droppingFirstFragment = start > 0;
    let droppingLongLine = false;
    let pending = '';
    let position = start;

    const tooLong = (line: string): boolean =>
      line.length > LINE_BYTES_PROBE_CHARS && Buffer.byteLength(line, 'utf8') > MAX_LINE_BYTES;

    while (position < size) {
      const want = Math.min(chunk.length, size - position);
      let read = 0;
      try {
        read = fs.readSync(fd, chunk, 0, want, position);
      } catch (error) {
        if (!isMissing(error)) result.error = new LogReadError(source, 'read', error);
        result.failed = true;
        break;
      }
      if (read <= 0) break;
      position += read;
      result.bytesRead += read;
      pending += decoder.write(chunk.subarray(0, read));

      for (;;) {
        const newline = pending.indexOf('\n');
        if (newline === -1) break;
        let line = pending.slice(0, newline);
        pending = pending.slice(newline + 1);
        if (droppingFirstFragment) {
          // Half a line that began before our window: not a record, not a skip.
          droppingFirstFragment = false;
          continue;
        }
        if (droppingLongLine) {
          droppingLongLine = false;
          continue;
        }
        if (line.endsWith('\r')) line = line.slice(0, -1);
        if (tooLong(line)) {
          result.skippedLines += 1;
          continue;
        }
        onLine(line);
      }

      if (droppingFirstFragment) {
        // Still inside the leading fragment and it is already huge: keep dropping.
        if (pending.length > LINE_BYTES_PROBE_CHARS) pending = '';
      } else if (!droppingLongLine && tooLong(pending)) {
        // No newline in sight and the carry already exceeds the limit: stop buffering.
        droppingLongLine = true;
        result.skippedLines += 1;
        pending = '';
      }
    }

    pending += decoder.end();
    if (!droppingFirstFragment && !droppingLongLine && pending) {
      const line = pending.endsWith('\r') ? pending.slice(0, -1) : pending;
      if (tooLong(line)) result.skippedLines += 1;
      else onLine(line);
    }
  } catch (error) {
    if (!isMissing(error)) result.error = new LogReadError(source, 'read', error);
    result.failed = true;
  } finally {
    try {
      fs.closeSync(fd);
    } catch {
      /* already closed / never opened */
    }
  }
  return result;
}

export interface ReadSourceOptions {
  /** Hard byte budget for this source, shared across its rotated files. */
  byteBudget: number;
  /** Drops entries while streaming, so filtered-out records are never accumulated. */
  accept?: (entry: LogEntry) => boolean;
  /** Rotated files are older, so export needs a per-source sort; the viewer sorts once at the end. */
  sort?: boolean;
  /** Viewer-only: a stable per-record id so duplicate content cannot collide in list keys. */
  withRecordId?: boolean;
}

/** Reads the active file plus rotated history for one source, newest first. */
function readSource(dir: string, source: string, options: ReadSourceOptions): SourceReadResult {
  const active = logFilePath(dir, source as LogSource);
  const result: SourceReadResult = {
    entries: [],
    bytes: 0,
    truncated: false,
    // Until at least one generation is read successfully, this source is
    // genuinely unavailable (all candidates may be ENOENT).
    unavailable: true,
    missing: false,
    skippedLines: 0,
    errors: [],
  };
  // `error` keeps the FIRST failure (queries throw it); `errors` collects them all
  // as basename + stage + normalized code, the only shape the export may disclose.
  const recordError = (file: string, failure: LogReadError): void => {
    result.error ??= failure;
    result.errors.push({ file, stage: failure.stage, code: failure.code });
  };
  // Windows may report ENOENT for a child of a regular file. Check the log
  // directory itself so that this invalid layout cannot look like missing logs.
  try {
    if (!fs.statSync(dir).isDirectory()) {
      recordError(path.basename(dir), new LogReadError(source, 'stat', { code: 'ENOTDIR' }));
      return result;
    }
  } catch (error) {
    if (isMissing(error)) {
      // The log directory itself is absent: every candidate is definitionally ENOENT.
      result.missing = true;
    } else {
      recordError(path.basename(dir), new LogReadError(source, 'stat', error));
    }
    return result;
  }
  // Read newest first: active file, then .1 … .N.
  const candidates = [active, ...Array.from({ length: LOG_BACKUP_COUNT }, (_, i) => `${active}.${i + 1}`)];
  let budget = options.byteBudget;
  let attempted = false;
  let readable = false;

  for (let fileIndex = 0; fileIndex < candidates.length; fileIndex += 1) {
    if (budget <= 0) {
      result.truncated = true;
      break;
    }
    const candidate = candidates[fileIndex];
    attempted = true;
    let size = 0;
    try {
      const stat = fs.statSync(candidate);
      if (!stat.isFile()) {
        recordError(path.basename(candidate), new LogReadError(source, 'stat', { code: 'EINVAL' }));
        continue;
      }
      size = stat.size;
    } catch (error) {
      // ENOENT is normal for an absent active/rotated generation. Other failures
      // must reach the strict query contract instead of becoming "no logs".
      if (isMissing(error)) continue;
      recordError(path.basename(candidate), new LogReadError(source, 'stat', error));
      continue;
    }
    if (!Number.isFinite(size) || size < 0) {
      recordError(path.basename(candidate), new LogReadError(source, 'stat', { code: 'EINVAL' }));
      continue;
    }
    // An existing empty file is readable too; still open it below so an ACL
    // failure on an empty file cannot be mistaken for a successful empty scan.
    const take = Math.min(size, budget);
    if (size > take) result.truncated = true;
    const start = size - take;

    let lineIndex = 0;
    const stream = streamLines(candidate, start, size, (line) => {
      lineIndex += 1;
      const entry = parseLogLine(line, source);
      if (!entry) return;
      if (options.accept && !options.accept(entry)) return;
      if (options.withRecordId) entry.record_id = `${source}#${fileIndex}#${lineIndex}`;
      result.entries.push(entry);
    }, source);

    budget -= stream.bytesRead;
    result.bytes += stream.bytesRead;
    result.skippedLines += stream.skippedLines;
    if (stream.error) {
      // Queries reject the saved error; exports still collect readable history.
      recordError(path.basename(candidate), stream.error);
      continue;
    }
    if (!stream.failed) readable = true;
  }

  if (!attempted || readable) result.unavailable = false;
  // Missing = every attempted candidate was absent: nothing read, nothing else
  // failed. Unscanned generations (budget exhausted first) are not evidence either way.
  result.missing = attempted && !readable && result.errors.length === 0;

  // Only export needs this order (its byte cap keeps the newest records); the viewer
  // sorts the merged result once instead of sorting every source and then sorting again.
  if (options.sort) {
    result.entries.sort((a, b) => b.timestamp_ms - a.timestamp_ms);
  }
  return result;
}

/** `needle` must already be lower-cased so the per-entry scan does not re-do it. */
function matchesKeyword(entry: LogEntry, needle: string): boolean {
  if (entry.message.toLowerCase().includes(needle)) return true;
  if (entry.event.toLowerCase().includes(needle)) return true;
  try {
    return JSON.stringify(entry).toLowerCase().includes(needle);
  } catch {
    return false;
  }
}

export function queryLogs(options: LogQueryOptions): LogQueryResult {
  const {
    dir,
    sources = ALL_SOURCES,
    from = 0,
    to = Number.MAX_SAFE_INTEGER,
    levels,
    sessionId,
    taskId,
    keyword,
    page = 1,
    pageSize = DEFAULT_PAGE_SIZE,
    maxScanBytes,
  } = options;

  const safePage = Math.max(1, Math.floor(page) || 1);
  const safePageSize = Math.min(500, Math.max(1, Math.floor(pageSize) || DEFAULT_PAGE_SIZE));
  const levelFilter = levels && levels.length ? new Set(levels.map((l) => l.toUpperCase())) : null;
  const needle = keyword ? keyword.toLowerCase() : null;

  const unavailable: string[] = [];
  const filtered: LogEntry[] = [];
  let scannedBytes = 0;
  let scanTruncated = false;
  let skippedLines = 0;
  let budget =
    Number.isFinite(maxScanBytes) && (maxScanBytes as number) > 0 ? Math.floor(maxScanBytes as number) : MAX_SCAN_BYTES;

  const accept = (entry: LogEntry): boolean => {
    if (entry.timestamp_ms < from || entry.timestamp_ms > to) return false;
    if (levelFilter && !levelFilter.has(entry.level.toUpperCase())) return false;
    if (sessionId && entry.session_id !== sessionId) return false;
    if (taskId && String(entry.task_id ?? '') !== taskId) return false;
    if (needle && !matchesKeyword(entry, needle)) return false;
    return true;
  };

  for (let i = 0; i < sources.length; i += 1) {
    const source = sources[i];
    const read = readSource(dir, source, { byteBudget: budget, accept, withRecordId: true });
    if (read.error) throw read.error;
    budget -= read.bytes;
    scannedBytes += read.bytes;
    skippedLines += read.skippedLines;
    if (read.truncated) scanTruncated = true;
    if (read.unavailable) unavailable.push(source);
    // Appended one by one on purpose: `filtered.push(...read.entries)` overflows the
    // call stack once a source contributes a few hundred thousand entries.
    for (let e = 0; e < read.entries.length; e += 1) filtered.push(read.entries[e]);
  }

  filtered.sort((a, b) => b.timestamp_ms - a.timestamp_ms);
  const start = (safePage - 1) * safePageSize;
  const entries = filtered.slice(start, start + safePageSize);

  return {
    ok: true,
    entries,
    total: filtered.length,
    page: safePage,
    pageSize: safePageSize,
    hasMore: start + safePageSize < filtered.length,
    scannedBytes,
    scanTruncated,
    unavailable,
    skippedLines,
  };
}

export interface ExportSelection {
  /** Per-source payloads destined for the ZIP, already scrubbed. */
  files: Array<{ name: string; content: string; entries: number; bytes: number }>;
  totalEntries: number;
  totalBytes: number;
  perSource: Record<string, number>;
  truncated: boolean;
  note: string;
  /** Lines dropped for exceeding MAX_LINE_BYTES; reported in the note so it is not silent. */
  skippedLines: number;
  /**
   * Every read failure across all sources, sanitized for disclosure:
   * source + basename + stage + normalized code — never an absolute path,
   * raw exception text or log-line content.
   */
  read_errors: Array<{ source: string; file: string; stage: 'stat' | 'open' | 'read'; code: string }>;
  /** Sources whose candidate generations were all absent (all ENOENT). */
  missing_sources: string[];
  /** Sources whose payload contains memory-ring or pending-queue records. */
  memory_sources: string[];
  /** Pending records actually written to the payload (after dedup, window and byte cap). */
  buffered_entries: number;
  /** Pending records dropped by the logger's bounded queue before export ran. */
  dropped_pending_entries: number;
  /** At least one source hit the scan byte budget before all generations were read. */
  read_truncated: boolean;
  /**
   * The payload is known to be incomplete or memory-sourced. Frozen truth table:
   * read_errors>0 || read_truncated || truncated || skippedLines>0 ||
   * memory_sources>0 || dropped_pending_entries>0.
   */
  incomplete: boolean;
}

/**
 * Selects and re-scrubs export payload for [from, to].
 * The 20 MiB cap keeps the NEWEST records and records the truncation in the note.
 *
 * Merge order per source: disk records → memoryFallback (desktop only, when the disk
 * produced nothing) → pendingEntries (the loggers' not-yet-persisted queue, grouped
 * by each record's own `source`). Pending records are de-duplicated against
 * already-collected ones via `log_record_id`; a pending record without an id is
 * always kept — better a duplicate than a gap.
 */
export function collectExportEntries(options: {
  dir: string;
  sources?: string[];
  from: number;
  to: number;
  memoryFallback?: Record<string, unknown>[];
  /** Merged `getPendingEntries()` snapshots; each record carries its own `source`. */
  pendingEntries?: Record<string, unknown>[];
  /** Sum of the loggers' droppedPending counters, disclosed verbatim in the manifest. */
  droppedPendingEntries?: number;
  maxRawBytes?: number;
  /** Overrides MAX_SCAN_BYTES — exists so tests can exercise the truncation path. */
  maxScanBytes?: number;
}): ExportSelection {
  const { dir, sources = ALL_SOURCES, from, to, memoryFallback = [], pendingEntries = [] } = options;
  const maxRaw = options.maxRawBytes ?? EXPORT_MAX_RAW_BYTES;
  const scanBudget =
    Number.isFinite(options.maxScanBytes) && (options.maxScanBytes as number) > 0
      ? Math.floor(options.maxScanBytes as number)
      : MAX_SCAN_BYTES;
  const droppedPending =
    Number.isFinite(options.droppedPendingEntries) && (options.droppedPendingEntries as number) > 0
      ? Math.floor(options.droppedPendingEntries as number)
      : 0;

  const perSourceEntries = new Map<string, LogEntry[]>();
  const readErrors: ExportSelection['read_errors'] = [];
  const missingSources: string[] = [];
  const memorySources: string[] = [];
  // Identity tracking: which entries came from memory / pending, so memory_sources
  // and buffered_entries reflect what the payload ACTUALLY contains after the cap.
  const memoryRingEntries = new Set<LogEntry>();
  const pendingSet = new Set<LogEntry>();
  let skippedLines = 0;
  let readTruncated = false;
  let bufferedEntries = 0;

  for (const source of sources) {
    const read = readSource(dir, source, {
      byteBudget: scanBudget,
      accept: (entry) => entry.timestamp_ms >= from && entry.timestamp_ms <= to,
      sort: true,
    });
    skippedLines += read.skippedLines;
    if (read.truncated) readTruncated = true;
    if (read.missing) missingSources.push(source);
    for (const failure of read.errors) {
      readErrors.push({ source, file: failure.file, stage: failure.stage, code: failure.code });
    }
    perSourceEntries.set(source, read.entries);
  }

  // Memory fallback: only used when a source produced nothing from disk (e.g. the log
  // directory was unwritable, which is exactly the startup-failure export case).
  let usedMemory = false;
  const diskDesktop = perSourceEntries.get('desktop') || [];
  if (diskDesktop.length === 0 && memoryFallback.length > 0) {
    const scrubbed = memoryFallback
      .map((item) => parseLogLine(JSON.stringify(item), 'desktop'))
      .filter((entry): entry is LogEntry => entry !== null)
      .filter((entry) => entry.timestamp_ms >= from && entry.timestamp_ms <= to);
    if (scrubbed.length) {
      for (const entry of scrubbed) memoryRingEntries.add(entry);
      perSourceEntries.set('desktop', scrubbed);
      usedMemory = true;
    }
  }

  // Pending-queue merge: grouped by each record's own `source`, deduplicated by
  // log_record_id against records already collected (disk + memory ring).
  const pendingBySource = new Map<string, Record<string, unknown>[]>();
  for (const item of pendingEntries) {
    if (!item || typeof item !== 'object') continue;
    const source = typeof item.source === 'string' ? item.source : '';
    if (!source || !sources.includes(source)) continue;
    const queued = pendingBySource.get(source) ?? [];
    queued.push(item);
    pendingBySource.set(source, queued);
  }
  for (const source of sources) {
    const queued = pendingBySource.get(source);
    if (!queued || queued.length === 0) continue;
    const collected = perSourceEntries.get(source) ?? [];
    const knownIds = new Set<string>();
    for (const item of collected) {
      if (typeof item.log_record_id === 'string' && item.log_record_id) knownIds.add(item.log_record_id);
    }
    for (const item of queued) {
      const entry = parseLogLine(JSON.stringify(item), source);
      if (!entry) continue;
      if (entry.timestamp_ms < from || entry.timestamp_ms > to) continue;
      const id = typeof entry.log_record_id === 'string' && entry.log_record_id ? entry.log_record_id : null;
      if (id && knownIds.has(id)) continue;
      pendingSet.add(entry);
      collected.push(entry);
    }
    perSourceEntries.set(source, collected);
  }

  // Pending and memory records must interleave by time, not append after the disk
  // tail; the disk segment is already newest-first, so one sort completes the merge.
  let totalAvailable = 0;
  for (const source of sources) {
    const entries = perSourceEntries.get(source);
    if (entries && entries.length > 1) entries.sort((a, b) => b.timestamp_ms - a.timestamp_ms);
    totalAvailable += entries ? entries.length : 0;
  }

  const files: ExportSelection['files'] = [];
  const perSource: Record<string, number> = {};
  let totalBytes = 0;
  let totalEntries = 0;
  let truncated = false;

  for (const source of sources) {
    const entries = perSourceEntries.get(source) || [];
    if (entries.length === 0) continue;
    const chunks: string[] = [];
    let bytes = 0;
    let kept = 0;
    let buffered = 0;
    let usedBuffer = false;
    for (const entry of entries) {
      const line = `${JSON.stringify(entry)}\n`;
      const lineBytes = Buffer.byteLength(line, 'utf8');
      if (totalBytes + bytes + lineBytes > maxRaw) {
        truncated = true;
        break;
      }
      chunks.push(line);
      bytes += lineBytes;
      kept += 1;
      if (pendingSet.has(entry)) {
        buffered += 1;
        usedBuffer = true;
      } else if (memoryRingEntries.has(entry)) {
        usedBuffer = true;
      }
    }
    if (kept === 0) continue;
    bufferedEntries += buffered;
    if (usedBuffer) memorySources.push(source);
    files.push({ name: `${source}.jsonl`, content: chunks.join(''), entries: kept, bytes });
    perSource[source] = kept;
    totalBytes += bytes;
    totalEntries += kept;
  }

  let note = `导出时段 ${new Date(from).toISOString()} ~ ${new Date(to).toISOString()}`;
  if (truncated) {
    note += `；原始日志超过 ${Math.round(maxRaw / (1024 * 1024))} MiB 上限，已仅保留最新记录`;
  }
  if (skippedLines > 0) {
    note += `；已跳过 ${skippedLines} 条超过 ${Math.round(MAX_LINE_BYTES / 1024)} KiB 的超长记录`;
  }
  if (usedMemory) {
    note += '；磁盘日志不可用，桌面日志取自当前进程内存缓冲';
  }
  if (readErrors.length > 0) {
    note += `；${readErrors.length} 个日志文件读取失败，详见 MANIFEST.json 的 read_errors`;
  }
  if (readTruncated) {
    note += '；日志扫描达到读取上限，部分记录未读取';
  }
  if (missingSources.length > 0) {
    note += `；以下来源无磁盘日志文件：${missingSources.join('、')}`;
  }
  if (bufferedEntries > 0) {
    note += `；包含 ${bufferedEntries} 条尚未落盘的内存待写记录`;
  }
  if (droppedPending > 0) {
    note += `；${droppedPending} 条待写记录因缓冲超限被丢弃`;
  }
  if (totalAvailable > totalEntries) {
    note += `；时段内共 ${totalAvailable} 条，已导出 ${totalEntries} 条`;
  }

  const incomplete =
    readErrors.length > 0 ||
    readTruncated ||
    truncated ||
    skippedLines > 0 ||
    memorySources.length > 0 ||
    droppedPending > 0;

  return {
    files,
    totalEntries,
    totalBytes,
    perSource,
    truncated,
    note,
    skippedLines,
    read_errors: readErrors,
    missing_sources: missingSources,
    memory_sources: memorySources,
    buffered_entries: bufferedEntries,
    dropped_pending_entries: droppedPending,
    read_truncated: readTruncated,
    incomplete,
  };
}
