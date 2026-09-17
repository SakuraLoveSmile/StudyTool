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
  return entry;
}

interface SourceReadResult {
  entries: LogEntry[];
  bytes: number;
  truncated: boolean;
  unavailable: boolean;
  skippedLines: number;
}

interface LineStreamResult {
  bytesRead: number;
  skippedLines: number;
  /** A read error mid-file (rotation, deletion) — the caller degrades, it never throws. */
  failed: boolean;
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
  onLine: (line: string) => void
): LineStreamResult {
  const result: LineStreamResult = { bytesRead: 0, skippedLines: 0, failed: false };
  let fd: number;
  try {
    fd = fs.openSync(file, 'r');
  } catch {
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
      } catch {
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
  } catch {
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
    unavailable: false,
    skippedLines: 0,
  };
  if (!fs.existsSync(active)) {
    result.unavailable = true;
    return result;
  }

  // Read newest first: active file, then .1 … .N.
  const candidates = [active, ...Array.from({ length: LOG_BACKUP_COUNT }, (_, i) => `${active}.${i + 1}`)];
  let budget = options.byteBudget;

  for (let fileIndex = 0; fileIndex < candidates.length; fileIndex += 1) {
    if (budget <= 0) {
      result.truncated = true;
      break;
    }
    const candidate = candidates[fileIndex];
    if (!fs.existsSync(candidate)) continue;

    let size = 0;
    try {
      size = fs.statSync(candidate).size;
    } catch {
      // Rotated away between the existence check and the stat: keep going.
      result.unavailable = true;
      continue;
    }
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
    });

    budget -= stream.bytesRead;
    result.bytes += stream.bytesRead;
    result.skippedLines += stream.skippedLines;
    if (stream.failed) result.unavailable = true;
  }

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
}

/**
 * Selects and re-scrubs export payload for [from, to].
 * The 20 MiB cap keeps the NEWEST records and records the truncation in the note.
 */
export function collectExportEntries(options: {
  dir: string;
  sources?: string[];
  from: number;
  to: number;
  memoryFallback?: Record<string, unknown>[];
  maxRawBytes?: number;
}): ExportSelection {
  const { dir, sources = ALL_SOURCES, from, to, memoryFallback = [] } = options;
  const maxRaw = options.maxRawBytes ?? EXPORT_MAX_RAW_BYTES;

  const perSourceEntries = new Map<string, LogEntry[]>();
  let totalAvailable = 0;
  let skippedLines = 0;

  for (const source of sources) {
    const read = readSource(dir, source, {
      byteBudget: MAX_SCAN_BYTES,
      accept: (entry) => entry.timestamp_ms >= from && entry.timestamp_ms <= to,
      sort: true,
    });
    skippedLines += read.skippedLines;
    totalAvailable += read.entries.length;
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
      perSourceEntries.set('desktop', scrubbed);
      usedMemory = true;
    }
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
    }
    if (kept === 0) continue;
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
  if (totalAvailable > totalEntries) {
    note += `；时段内共 ${totalAvailable} 条，已导出 ${totalEntries} 条`;
  }

  return { files, totalEntries, totalBytes, perSource, truncated, note, skippedLines };
}
