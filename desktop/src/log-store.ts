/**
 * Log reader / normalizer for the desktop process.
 *
 * One reader serves both the in-app viewer (IPC query) and the diagnostic export (ZIP),
 * so "what you see" and "what you send" can never diverge.
 *
 * Two gates live here:
 *  - legacy compatibility: old records (no source / asctime / non-JSON lines) are
 *    normalized instead of being dropped;
 *  - export re-scrubbing: every entry is passed through the whitelist + scrubber again
 *    on read, so a file written by an older build cannot leak through the export path.
 */

import fs from 'fs';
import path from 'path';
import { FIELD_WHITELIST, LOG_BACKUP_COUNT, scrubRecord, scrubString, logFilePath, type LogSource } from './diagnostics';

export const ALL_SOURCES: LogSource[] = ['desktop', 'backend', 'frontend'];
export const DEFAULT_PAGE_SIZE = 200;
/** Never read more than this much raw text per query/export, to bound memory and latency. */
const MAX_SCAN_BYTES = 48 * 1024 * 1024;
/** Export cap for raw log payload (release gate: 20 MiB, newest records win). */
export const EXPORT_MAX_RAW_BYTES = 20 * 1024 * 1024;

export interface LogEntry {
  timestamp: string;
  timestamp_ms: number;
  level: string;
  source: string;
  event: string;
  message: string;
  session_id: string | null;
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
}

export interface LogQueryResult {
  ok: boolean;
  entries: LogEntry[];
  total: number;
  page: number;
  pageSize: number;
  hasMore: boolean;
  scannedBytes: number;
  scanTruncated: boolean;
  unavailable: string[];
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
}

/** Reads the active file plus rotated history for one source, newest first. */
function readSource(dir: string, source: string, byteBudget: number): SourceReadResult {
  const active = logFilePath(dir, source as LogSource);
  const result: SourceReadResult = { entries: [], bytes: 0, truncated: false, unavailable: false };
  if (!fs.existsSync(active)) {
    result.unavailable = true;
    return result;
  }

  // Read newest first: active file, then .1 … .N.
  const candidates = [active, ...Array.from({ length: LOG_BACKUP_COUNT }, (_, i) => `${active}.${i + 1}`)];
  let budget = byteBudget;
  const collected: LogEntry[] = [];
  let sawBytes = 0;

  for (const candidate of candidates) {
    if (budget <= 0) {
      result.truncated = true;
      break;
    }
    if (!fs.existsSync(candidate)) continue;
    try {
      const stat = fs.statSync(candidate);
      const take = Math.min(stat.size, budget);
      if (stat.size > take) result.truncated = true;
      const start = stat.size - take;
      const fd = fs.openSync(candidate, 'r');
      let raw = '';
      try {
        const buffer = Buffer.alloc(take);
        fs.readSync(fd, buffer, 0, take, start);
        raw = buffer.toString('utf8');
      } finally {
        fs.closeSync(fd);
      }
      budget -= take;
      sawBytes += take;
      // A partial read can start mid-line; drop the first fragment when we skipped bytes.
      const lines = raw.split(/\r?\n/);
      if (start > 0) lines.shift();
      for (const line of lines) {
        const entry = parseLogLine(line, source);
        if (entry) collected.push(entry);
      }
    } catch {
      result.unavailable = true;
    }
  }

  // Rotated files are older; sorting by time yields a correct newest-first view.
  collected.sort((a, b) => b.timestamp_ms - a.timestamp_ms);
  result.entries = collected;
  result.bytes = sawBytes;
  return result;
}

function matchesKeyword(entry: LogEntry, keyword: string): boolean {
  const needle = keyword.toLowerCase();
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
  } = options;

  const safePage = Math.max(1, Math.floor(page) || 1);
  const safePageSize = Math.min(500, Math.max(1, Math.floor(pageSize) || DEFAULT_PAGE_SIZE));
  const levelFilter = levels && levels.length ? new Set(levels.map((l) => l.toUpperCase())) : null;

  const unavailable: string[] = [];
  const all: LogEntry[] = [];
  let scannedBytes = 0;
  let scanTruncated = false;
  let budget = MAX_SCAN_BYTES;

  for (const source of sources) {
    const read = readSource(dir, source, budget);
    budget -= read.bytes;
    scannedBytes += read.bytes;
    if (read.truncated) scanTruncated = true;
    if (read.unavailable) unavailable.push(source);
    all.push(...read.entries);
  }

  const filtered = all.filter((entry) => {
    if (entry.timestamp_ms < from || entry.timestamp_ms > to) return false;
    if (levelFilter && !levelFilter.has(entry.level.toUpperCase())) return false;
    if (sessionId && entry.session_id !== sessionId) return false;
    if (taskId && String((entry as any).task_id ?? '') !== taskId) return false;
    if (keyword && !matchesKeyword(entry, keyword)) return false;
    return true;
  });

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

  for (const source of sources) {
    const read = readSource(dir, source, MAX_SCAN_BYTES);
    const inWindow = read.entries.filter((e) => e.timestamp_ms >= from && e.timestamp_ms <= to);
    totalAvailable += inWindow.length;
    perSourceEntries.set(source, inWindow);
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
  if (usedMemory) {
    note += '；磁盘日志不可用，桌面日志取自当前进程内存缓冲';
  }
  if (totalAvailable > totalEntries) {
    note += `；时段内共 ${totalAvailable} 条，已导出 ${totalEntries} 条`;
  }

  return { files, totalEntries, totalBytes, perSource, truncated, note };
}
