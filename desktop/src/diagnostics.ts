/**
 * Desktop-side structured diagnostics writer (JSONL).
 *
 * Design rules (must stay true, they are release gates):
 * 1. Format is shared with the backend (`backend/app/diagnostics.py`) so one reader can
 *    parse all three files: timestamp / level / source / event / message / session_id /
 *    app_version plus correlation fields.
 * 2. A whitelist decides which fields may EVER be persisted; unknown keys are dropped,
 *    and every surviving string value is scrubbed (secrets, URL query strings, user
 *    paths, document file names).
 * 3. Logging must never break the product: every write is fail-safe, rate limited and
 *    duplicate-aggregated. A broken log directory degrades to an in-memory ring
 *    buffer plus a bounded pending-write queue that is replayed once writes
 *    succeed again.
 */

import fs from 'fs';
import path from 'path';

export type LogSource = 'desktop' | 'backend' | 'frontend';
export type LogLevel = 'DEBUG' | 'INFO' | 'WARN' | 'ERROR';

/** 5 MiB per file, 5 rotated files, 7 day retention (release-gated numbers). */
export const LOG_MAX_BYTES = 5 * 1024 * 1024;
export const LOG_BACKUP_COUNT = 5;
export const LOG_RETENTION_DAYS = 7;

/** Hard caps so one bad caller cannot blow up a log file. */
const MAX_STRING_LENGTH = 2000;
const MAX_MESSAGE_LENGTH = 500;
/**
 * Strings above this are never handed to the scrubber at all. Every pattern below is
 * linear in the text it scans, so this ceiling is what keeps one pathological record
 * (a 10 MiB stack trace, a minified bundle dumped into a field) from stalling the
 * whole process — and it keeps the fallback cost of a regex misbehaving bounded.
 */
export const MAX_SCRUB_STRING_LENGTH = 8 * 1024;
/**
 * Replacement for an oversized string. Deliberately NOT a prefix of the original:
 * an unscrubbed prefix must never reach the log file, the viewer or the export bundle.
 */
export const OMITTED_STRING_MARKER = '…<omitted: oversized string>';
const MAX_ARRAY_ITEMS = 20;
const MAX_DEPTH = 4;
const MEMORY_RING_SIZE = 500;
/**
 * Pending-write queue bounds (beta.3 D2): whichever limit is reached first applies.
 * Bytes are counted per JSONL line in UTF-8 (`JSON.stringify(record) + '\n'`).
 * Overflow evicts the OLDEST pending record and permanently increments
 * `droppedPending` — it is never reset.
 */
export const PENDING_MAX_ENTRIES = 500;
export const PENDING_MAX_BYTES = 1024 * 1024;

/**
 * Only these field names may be persisted. Anything else is dropped before writing,
 * which is what makes "never record business payload" enforceable rather than aspirational.
 */
export const FIELD_WHITELIST: ReadonlySet<string> = new Set([
  // core envelope
  'timestamp', 'level', 'source', 'event', 'message', 'session_id', 'instance_id',
  'app_version', 'platform', 'arch', 'logger', 'phase', 'pid', 'log_record_id',
  // errors / failures
  'error_type', 'error_name', 'error_message', 'stack', 'stack_frames', 'frames',
  'cause_chain', 'exception_type', 'failed_stage', 'stage', 'reason', 'detail',
  'component', 'info', 'reason_code',
  // stack frame shape (exception_info.frames[]) — without these the frames arrive empty
  'file', 'line', 'func',
  // correlation (shared with backend task/unit/call tracing)
  'task_id', 'task_type', 'unit_id', 'unit_index', 'scope', 'call_id', 'attempt',
  // external calls
  'service', 'model', 'endpoint', 'method', 'status_code', 'provider_request_id',
  'duration_ms', 'timeout_config', 'retry_reason', 'retry_wait_seconds',
  'input_chars', 'input_bytes', 'result_count',
  // process lifecycle
  'exit_code', 'signal', 'code', 'killed', 'code_name',
  // limiting / aggregation
  'occurrences', 'first_seen', 'last_seen', 'dropped', 'limit', 'window_ms',
  // misc counters
  'count', 'total', 'ok', 'name', 'id', 'version', 'bytes', 'entries', 'truncated',
  'note', 'action', 'operation', 'route', 'target', 'kind', 'tags', 'has_more',
  'page', 'page_size', 'sources', 'levels',
  // log-navigation diagnostics (T2a): correlation id + phase details — dropping
  // these silently would sever the click→guard→mount→query evidence chain.
  'nav_id', 'trigger', 'to_name', 'elapsed_ms',
]);

const SECRET_PATTERNS: Array<[RegExp, string]> = [
  // Word-boundary anchored: an identifier such as `task-recon-999` merely CONTAINS "sk-"
  // and must not be mangled, while real keys (space/quote/line prefixed) are masked.
  [/(?<![A-Za-z0-9_])sk-([A-Za-z0-9_-]{4})[A-Za-z0-9_-]+/g, 'sk-$1***'],
  [/(bearer\s+)[A-Za-z0-9_.\-]+/gi, '$1***'],
  [
    /((?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|authorization|secret|password|cookie|session[_-]?token)\s*[:=]\s*)\S+/gi,
    '$1***',
  ],
  [/https?:\/\/[^:@\s/]+:[^:@\s/]+@/gi, 'https://***:***@'],
];

/** Absolute user paths: drive letters, UNC, macOS/Linux home, and `~`. */
const USER_PATH_PATTERNS: RegExp[] = [
  /[A-Za-z]:\\(?:[^\\\s"']{1,255}\\)*[^\\\s"']{0,255}/g,
  /\\\\[^\s"']{1,255}/g,
  // Anchored on `\/(Users|home)\/`, then each further segment requires its own `/`.
  // The segment body excludes `/`, so the group cannot split one path several ways —
  // that ambiguity is what used to make a long path an exponential backtracking trap.
  /\/(?:Users|home)\/[^\s"':/]{1,255}(?:\/[^\s"':/]{1,255})*/g,
  /(?:\/Users|\/home)\/[^\s/]{1,255}/g,
  /~\/[^\s"']{0,255}/g,
  /(?:AppData|LOCALAPPDATA|APPDATA)[\\/][^\s"']{0,255}/gi,
];

/**
 * Document/media file names are user content — never persist them.
 * Source-code extensions are deliberately excluded: module names such as `llm.py`
 * are legitimate diagnostics and carry no user data.
 *
 * The base-name run is length-bounded on purpose. With an unbounded `+` the engine
 * retries every prefix of a long separator-free run at every offset, which is
 * quadratic: a single 200 KiB minified line was enough to freeze the window. 255
 * characters is the Windows file-name limit, so nothing real is missed.
 */
const DOC_FILENAME = /[^\s"'\\/:*?<>|]{1,255}\.(?:pdf|docx?|pptx?|xlsx?|csv|md|markdown|txt|rtf|png|jpe?g|webp|gif|bmp|tiff?|zip|rar|7z|tar|tgz|gz|epub|mobi)\b/gi;

/** URLs: keep scheme/host/path (ids templated), always strip query and fragment. */
function scrubUrl(raw: string): string {
  try {
    const parsed = new URL(raw);
    let p = parsed.pathname
      .replace(/\/[0-9a-fA-F-]{8,36}/g, '/{id}')
      .replace(/\/\d{4,}/g, '/{id}');
    return `${parsed.protocol}//${parsed.host}${p}`;
  } catch {
    return raw;
  }
}

const URL_PATTERN = /\bhttps?:\/\/[^\s"'<>]+/gi;

/** Scrubs one string value: secrets, URLs, user paths and document file names. */
export function scrubString(input: unknown): string {
  if (input === null || input === undefined) return '';
  const raw = typeof input === 'string' ? input : String(input);
  // Size gate BEFORE any regex runs: this is the difference between "one big record
  // costs a few milliseconds" and "one big record freezes the process".
  if (raw.length > MAX_SCRUB_STRING_LENGTH) return OMITTED_STRING_MARKER;
  let s = raw;
  s = s.replace(URL_PATTERN, (match) => scrubUrl(match));
  for (const [pattern, repl] of SECRET_PATTERNS) {
    s = s.replace(pattern, repl);
  }
  for (const pattern of USER_PATH_PATTERNS) {
    s = s.replace(pattern, '<path>');
  }
  s = s.replace(DOC_FILENAME, '<file>');
  if (s.length > MAX_STRING_LENGTH) {
    s = `${s.slice(0, MAX_STRING_LENGTH)}…<truncated>`;
  }
  return s;
}

/**
 * Recursively whitelists + scrubs a record. Returns a NEW object; the input is untouched.
 * Arrays keep only primitives (bounded), objects recurse (bounded depth).
 */
export function scrubRecord(input: unknown, depth = 0): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  if (depth > MAX_DEPTH || input === null || typeof input !== 'object') return out;
  for (const [key, value] of Object.entries(input as Record<string, unknown>)) {
    if (!FIELD_WHITELIST.has(key)) continue;
    if (value === null || value === undefined) continue;
    if (typeof value === 'number' || typeof value === 'boolean') {
      out[key] = value;
    } else if (typeof value === 'string') {
      out[key] = key === 'message' ? scrubString(value).slice(0, MAX_MESSAGE_LENGTH) : scrubString(value);
    } else if (Array.isArray(value)) {
      out[key] = value
        .slice(0, MAX_ARRAY_ITEMS)
        .map((item) =>
          typeof item === 'object' && item !== null ? scrubRecord(item, depth + 1) : scrubString(item)
        );
    } else if (typeof value === 'object') {
      out[key] = scrubRecord(value, depth + 1);
    }
  }
  if (typeof out.message === 'string' && out.message.length > MAX_MESSAGE_LENGTH) {
    out.message = out.message.slice(0, MAX_MESSAGE_LENGTH);
  }
  return out;
}

export function logFilePath(dir: string, source: LogSource): string {
  return path.join(dir, `${source}.jsonl`);
}

/** Rotated file names, oldest first: `desktop.jsonl.5` … `desktop.jsonl.1`. */
function rotatedPaths(file: string, backupCount: number): string[] {
  const out: string[] = [];
  for (let i = 1; i <= backupCount; i += 1) out.push(`${file}.${i}`);
  return out;
}

export interface DiagnosticsOptions {
  /** Log directory. Created if missing. */
  dir: string;
  source: LogSource;
  sessionId: string;
  appVersion: string;
  maxBytes?: number;
  backupCount?: number;
  retentionDays?: number;
  /** Token bucket size; excess records are dropped (and counted), never queued forever. */
  maxRecordsPerSecond?: number;
  /** Duplicate error aggregation window. */
  aggregationWindowMs?: number;
  maxTrackedKeys?: number;
  now?: () => number;
  /**
   * Internal/testing only: cadence of recovery attempts while degraded
   * (default 3000 ms). Not exposed over IPC.
   */
  recoveryIntervalMs?: number;
}

export interface LogRecordInput {
  event: string;
  message?: string;
  level?: LogLevel;
  [field: string]: unknown;
}

let stderrNotified = false;
function notifyOnce(message: string): void {
  if (stderrNotified) return;
  stderrNotified = true;
  try {
    process.stderr.write(`[DIAGNOSTICS] ${message}\n`);
  } catch {
    /* nothing else we can do */
  }
}

/**
 * Append-only JSONL writer with rotation, retention cleanup, rate limiting and
 * duplicate aggregation. Every public method is fail-safe: it never throws.
 */
export class DiagnosticsLogger {
  private readonly dir: string;
  private readonly source: LogSource;
  private readonly file: string;
  private readonly sessionId: string;
  private readonly appVersion: string;
  private readonly maxBytes: number;
  private readonly backupCount: number;
  private readonly retentionDays: number;
  private readonly maxRecordsPerSecond: number;
  private readonly aggregationWindowMs: number;
  private readonly maxTrackedKeys: number;
  private readonly now: () => number;
  /** Internal/testing only: recovery-attempt cadence while degraded. */
  private readonly recoveryIntervalMs: number;

  private currentSize = 0;
  private degraded = false;
  private closed = false;
  /** True once initialize() (or the equivalent recovery probe) has succeeded. */
  private initialized = false;
  /** Monotonic per-instance counter backing `log_record_id`. */
  private recordSeq = 0;

  private tokenBucket: number;
  private lastRefill: number;
  private droppedInWindow = 0;
  private dropReported = false;

  private readonly pending = new Map<
    string,
    { template: Record<string, unknown>; count: number; firstSeen: string; lastSeen: string; timer: NodeJS.Timeout }
  >();
  private readonly memory: Record<string, unknown>[] = [];
  private extraSources: LogSource[] = [];

  /**
   * Records accepted while degraded but not yet persisted. Entries carry their
   * already-minted `log_record_id`; they are removed only after a successful
   * replay (or when evicted as the oldest under the queue bounds).
   */
  private readonly pendingWrites: Array<{ record: Record<string, unknown>; bytes: number }> = [];
  private pendingBytes = 0;
  private droppedPending = 0;
  private recoveryTimer: NodeJS.Timeout | null = null;

  constructor(options: DiagnosticsOptions) {
    this.dir = options.dir;
    this.source = options.source;
    this.file = logFilePath(this.dir, options.source);
    this.sessionId = options.sessionId;
    this.appVersion = options.appVersion;
    this.maxBytes = options.maxBytes ?? LOG_MAX_BYTES;
    this.backupCount = options.backupCount ?? LOG_BACKUP_COUNT;
    this.retentionDays = options.retentionDays ?? LOG_RETENTION_DAYS;
    this.maxRecordsPerSecond = options.maxRecordsPerSecond ?? 200;
    this.aggregationWindowMs = options.aggregationWindowMs ?? 5000;
    this.maxTrackedKeys = options.maxTrackedKeys ?? 200;
    this.now = options.now ?? (() => Date.now());
    this.recoveryIntervalMs = options.recoveryIntervalMs ?? 3000;
    this.tokenBucket = this.maxRecordsPerSecond;
    this.lastRefill = this.now();
    this.initialize();
    // If initialize() already failed we are degraded from birth: start the
    // recovery cadence immediately so queued records can still reach disk later.
    this.ensureRecoveryTimer();
  }

  private initialize(): void {
    try {
      fs.mkdirSync(this.dir, { recursive: true });
      this.currentSize = fs.existsSync(this.file) ? fs.statSync(this.file).size : 0;
      this.cleanupOldFiles();
      this.initialized = true;
    } catch (err: any) {
      this.degraded = true;
      notifyOnce(`cannot initialize log file (${err?.message || err}); continuing with in-memory buffer only`);
    }
  }

  /** Deletes rotated files older than the retention window. Never touches the active file. */
  public cleanupOldFiles(): number {
    let removed = 0;
    try {
      const cutoff = this.now() - this.retentionDays * 24 * 60 * 60 * 1000;
      for (const candidate of rotatedPaths(this.file, this.backupCount)) {
        if (!fs.existsSync(candidate)) continue;
        if (fs.statSync(candidate).mtimeMs < cutoff) {
          fs.unlinkSync(candidate);
          removed += 1;
        }
      }
    } catch (err: any) {
      notifyOnce(`log retention cleanup failed: ${err?.message || err}`);
    }
    return removed;
  }

  public isDegraded(): boolean {
    return this.degraded;
  }

  public getLogDir(): string {
    return this.dir;
  }

  public getSessionId(): string {
    return this.sessionId;
  }

  /** In-memory ring buffer, used as an export fallback when disk writes fail. */
  public getMemoryEntries(): Record<string, unknown>[] {
    return [...this.memory];
  }

  /**
   * Snapshot of queued-but-not-yet-persisted records, in original order. Returns
   * a shallow copy per record — mutating the result never affects the queue.
   * Only queued records are included (never disk or ring-only records).
   */
  public getPendingEntries(): Record<string, unknown>[] {
    return this.pendingWrites.map((item) => ({ ...item.record }));
  }

  /** Persistence health — exactly these three fields, nothing else. */
  public getPersistenceState(): { degraded: boolean; pendingCount: number; droppedPending: number } {
    return {
      degraded: this.degraded,
      pendingCount: this.pendingWrites.length,
      droppedPending: this.droppedPending,
    };
  }

  public setExtraSources(sources: LogSource[]): void {
    this.extraSources = sources;
  }

  public getExtraSources(): LogSource[] {
    return [...this.extraSources];
  }

  private refillBucket(): void {
    const now = this.now();
    const elapsed = now - this.lastRefill;
    if (elapsed <= 0) return;
    const refill = (elapsed / 1000) * this.maxRecordsPerSecond;
    this.tokenBucket = Math.min(this.maxRecordsPerSecond, this.tokenBucket + refill);
    this.lastRefill = now;
    if (this.tokenBucket >= this.maxRecordsPerSecond * 0.5) {
      this.droppedInWindow = 0;
      this.dropReported = false;
    }
  }

  private takeToken(): boolean {
    this.refillBucket();
    if (this.tokenBucket < 1) {
      this.droppedInWindow += 1;
      return false;
    }
    this.tokenBucket -= 1;
    return true;
  }

  private rotateIfNeeded(incomingBytes: number): void {
    if (this.currentSize + incomingBytes <= this.maxBytes) return;
    const rotated = rotatedPaths(this.file, this.backupCount);
    try {
      const oldest = rotated[rotated.length - 1];
      if (oldest && fs.existsSync(oldest)) fs.unlinkSync(oldest);
      for (let i = rotated.length - 1; i > 0; i -= 1) {
        const from = rotated[i - 1];
        const to = rotated[i];
        if (fs.existsSync(from)) fs.renameSync(from, to);
      }
      if (fs.existsSync(this.file)) fs.renameSync(this.file, rotated[0]);
      this.currentSize = 0;
      this.cleanupOldFiles();
    } catch (err: any) {
      notifyOnce(`log rotation failed: ${err?.message || err}`);
    }
  }

  /**
   * Single funnel for every record that reaches the persistence path (first
   * writes, aggregate summaries, rate-limit notices, recovery summary). Mints a
   * fresh `log_record_id` for each one — the id travels with the record into the
   * memory ring and, while degraded, into the pending-write queue.
   */
  private writeEntry(entry: Record<string, unknown>): void {
    this.recordSeq += 1;
    entry.log_record_id = `${this.sessionId}:${this.source}:${String(this.recordSeq).padStart(8, '0')}`;
    this.memory.push(entry);
    if (this.memory.length > MEMORY_RING_SIZE) this.memory.shift();
    if (this.closed) return;
    // Already degraded (including never-initialized): queue for later replay.
    if (this.degraded) {
      this.enqueuePending(entry);
      return;
    }

    let line: string;
    try {
      line = `${JSON.stringify(entry)}\n`;
    } catch {
      return;
    }
    const bytes = Buffer.byteLength(line, 'utf8');
    try {
      this.rotateIfNeeded(bytes);
      fs.appendFileSync(this.file, line, { encoding: 'utf8' });
      this.currentSize += bytes;
    } catch (err: any) {
      this.degraded = true;
      // The record that failed to append is queued as well — nothing is lost.
      this.enqueuePending(entry);
      notifyOnce(`log write failed (${err?.message || err}); switching to in-memory buffer`);
      this.ensureRecoveryTimer();
    }
  }

  /**
   * Appends a record to the pending-write queue, then evicts the OLDEST entries
   * while either bound is exceeded (entry count first-hit or byte budget — both
   * are enforced every enqueue). `droppedPending` only ever grows.
   */
  private enqueuePending(entry: Record<string, unknown>): void {
    let line: string;
    try {
      line = `${JSON.stringify(entry)}\n`;
    } catch {
      return; // not serializable — it could never be persisted anyway
    }
    const bytes = Buffer.byteLength(line, 'utf8');
    this.pendingWrites.push({ record: entry, bytes });
    this.pendingBytes += bytes;
    while (this.pendingWrites.length > PENDING_MAX_ENTRIES || this.pendingBytes > PENDING_MAX_BYTES) {
      const evicted = this.pendingWrites.shift();
      if (!evicted) break;
      this.pendingBytes -= evicted.bytes;
      this.droppedPending += 1;
    }
  }

  /**
   * Replays one queued record through the normal rotate+append path WITHOUT
   * minting a new id or touching the memory ring (the record already lives
   * there). Returns false on the first write failure so callers can stop.
   */
  private replayPending(item: { record: Record<string, unknown>; bytes: number }): boolean {
    try {
      const line = `${JSON.stringify(item.record)}\n`;
      this.rotateIfNeeded(item.bytes);
      fs.appendFileSync(this.file, line, { encoding: 'utf8' });
      this.currentSize += item.bytes;
      return true;
    } catch {
      return false;
    }
  }

  /** Starts the recovery cadence once degraded. Idempotent; never blocks exit. */
  private ensureRecoveryTimer(): void {
    if (this.recoveryTimer || this.closed || !this.degraded) return;
    this.recoveryTimer = setInterval(() => this.attemptRecovery(), this.recoveryIntervalMs);
    if (typeof this.recoveryTimer.unref === 'function') this.recoveryTimer.unref();
  }

  private stopRecoveryTimer(): void {
    if (!this.recoveryTimer) return;
    try {
      clearInterval(this.recoveryTimer);
    } catch {
      /* ignore */
    }
    this.recoveryTimer = null;
  }

  /**
   * One recovery attempt (timer tick):
   * 1. if initialize() never succeeded, probe with the equivalent (mkdir +
   *    active file size read) — writes nothing; probe failure ends the attempt;
   * 2. if the queue is non-empty, replay it in order through the normal
   *    rotate+append path, stopping at the first failure;
   * 3. an empty queue (drained or already empty) clears `degraded` — a completed
   *    drain is summarised by a `log.persistence.recovered` record written
   *    through the normal path (fresh `log_record_id`).
   */
  private attemptRecovery(): void {
    try {
      if (this.closed || !this.degraded) {
        this.stopRecoveryTimer();
        return;
      }
      if (!this.initialized) {
        try {
          fs.mkdirSync(this.dir, { recursive: true });
          this.currentSize = fs.existsSync(this.file) ? fs.statSync(this.file).size : 0;
          this.initialized = true;
        } catch {
          return; // still broken — try again on the next tick
        }
      }
      let drained = 0;
      while (this.pendingWrites.length > 0) {
        const item = this.pendingWrites[0];
        if (!this.replayPending(item)) break;
        this.pendingWrites.shift();
        this.pendingBytes -= item.bytes;
        drained += 1;
      }
      if (this.pendingWrites.length > 0) return; // still degraded, keep retrying
      this.degraded = false;
      this.stopRecoveryTimer();
      if (drained > 0) {
        this.writeEntry(
          scrubRecord({
            timestamp: new Date(this.now()).toISOString(),
            level: 'INFO',
            source: this.source,
            event: 'log.persistence.recovered',
            message: '日志持久化已恢复，待写记录补写完成',
            session_id: this.sessionId,
            app_version: this.appVersion,
            entries: drained,
            dropped: this.droppedPending,
          })
        );
      }
    } catch {
      /* recovery must never throw */
    }
  }

  /** Core entry point. Never throws. */
  public log(level: LogLevel, event: string, message = '', fields: Record<string, unknown> = {}): void {
    try {
      if (this.closed) return;

      const scrubbed = scrubRecord({
        ...fields,
        timestamp: new Date(this.now()).toISOString(),
        level,
        source: this.source,
        event: scrubString(event),
        message: scrubString(message),
        session_id: this.sessionId,
        app_version: this.appVersion,
        platform: process.platform,
        arch: process.arch,
        pid: process.pid,
      });

      // Duplicate aggregation covers WARN/ERROR — the realistic flood sources (renderer
      // error loops, repeatedly failing calls). The FIRST occurrence is never delayed or
      // absorbed: it is written immediately with full context. Duplicates are summarised
      // at window close with occurrences / first_seen / last_seen. Everything is still
      // bounded by the rate limiter below, so INFO floods cannot run away either.
      const aggregatable = level === 'WARN' || level === 'ERROR';
      const key = `${level}|${String(scrubbed.event)}|${String(scrubbed.message)}|${String(scrubbed.task_id || '')}`;

      if (aggregatable) {
        const existing = this.pending.get(key);
        if (existing) {
          existing.count += 1;
          existing.lastSeen = String(scrubbed.timestamp);
          return;
        }
        if (this.pending.size >= this.maxTrackedKeys) {
          this.flushAggregated();
        }
      }

      if (!this.takeToken()) {
        if (!this.dropReported) {
          this.dropReported = true;
          this.writeEntry(
            scrubRecord({
              timestamp: new Date(this.now()).toISOString(),
              level: 'WARN',
              source: this.source,
              event: 'log.rate_limited',
              message: '日志写入超过限流阈值，超出部分已丢弃',
              session_id: this.sessionId,
              app_version: this.appVersion,
              dropped: this.droppedInWindow,
              limit: this.maxRecordsPerSecond,
              window_ms: 1000,
            })
          );
        }
        return;
      }

      this.writeEntry(scrubbed);

      if (aggregatable) {
        const timer = setTimeout(() => this.flushKey(key), this.aggregationWindowMs);
        if (typeof timer.unref === 'function') timer.unref();
        this.pending.set(key, {
          template: scrubbed,
          count: 1,
          firstSeen: String(scrubbed.timestamp),
          lastSeen: String(scrubbed.timestamp),
          timer,
        });
      }
    } catch (err: any) {
      notifyOnce(`log() failed: ${err?.message || err}`);
    }
  }

  private flushKey(key: string): void {
    const item = this.pending.get(key);
    if (!item) return;
    this.pending.delete(key);
    try {
      clearTimeout(item.timer);
    } catch {
      /* ignore */
    }
    // A single occurrence was already written in full when it arrived.
    if (item.count <= 1) return;
    this.writeEntry(
      scrubRecord({
        ...item.template,
        message: `重复错误已聚合（共 ${item.count} 次）`,
        occurrences: item.count,
        first_seen: item.firstSeen,
        last_seen: item.lastSeen,
      })
    );
  }

  /** Writes every aggregated-but-unflushed duplicate summary. Safe to call often. */
  public flushAggregated(): void {
    for (const key of [...this.pending.keys()]) {
      this.flushKey(key);
    }
  }

  public info(event: string, message = '', fields: Record<string, unknown> = {}): void {
    this.log('INFO', event, message, fields);
  }

  public warn(event: string, message = '', fields: Record<string, unknown> = {}): void {
    this.log('WARN', event, message, fields);
  }

  public error(event: string, message = '', fields: Record<string, unknown> = {}): void {
    this.log('ERROR', event, message, fields);
  }

  public debug(event: string, message = '', fields: Record<string, unknown> = {}): void {
    this.log('DEBUG', event, message, fields);
  }

  public close(): void {
    try {
      this.flushAggregated();
    } catch {
      /* ignore */
    }
    this.stopRecoveryTimer();
    // One synchronous best-effort pass over whatever is still queued: ordered,
    // stops at the first failure, never blocks and never throws.
    try {
      while (this.pendingWrites.length > 0) {
        const item = this.pendingWrites[0];
        if (!this.replayPending(item)) break;
        this.pendingWrites.shift();
        this.pendingBytes -= item.bytes;
      }
    } catch {
      /* ignore */
    }
    this.closed = true;
  }
}

let globalLogger: DiagnosticsLogger | null = null;

/** Process-wide desktop logger. Set once during startup, before the backend spawns. */
export function initDiagnostics(options: DiagnosticsOptions): DiagnosticsLogger {
  globalLogger = new DiagnosticsLogger(options);
  return globalLogger;
}

export function getDiagnostics(): DiagnosticsLogger | null {
  return globalLogger;
}

/** Fail-safe helper for code paths that may run before initialization. */
export function diag(level: LogLevel, event: string, message = '', fields: Record<string, unknown> = {}): void {
  try {
    globalLogger?.log(level, event, message, fields);
  } catch {
    /* logging must never break business flow */
  }
}

export function diagInfo(event: string, message = '', fields: Record<string, unknown> = {}): void {
  diag('INFO', event, message, fields);
}

export function diagWarn(event: string, message = '', fields: Record<string, unknown> = {}): void {
  diag('WARN', event, message, fields);
}

export function diagError(event: string, message = '', fields: Record<string, unknown> = {}): void {
  diag('ERROR', event, message, fields);
}
