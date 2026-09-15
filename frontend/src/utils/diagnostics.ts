/**
 * Frontend diagnostics reporter (test build).
 *
 * Scope is deliberately narrow, and that narrowness is a requirement:
 *  - collects Vue component errors and unhandled promise rejections ONLY;
 *  - never patches `console.*`, so ordinary console output is not harvested;
 *  - never talks HTTP. Reports go to the main process over the restricted
 *    `desktop:reportLog` IPC channel, where they are whitelisted, scrubbed and written
 *    to `frontend.jsonl`. In a plain browser build this module is a no-op.
 *
 * Volume control happens here as well as in the main process: a bounded queue, a rate
 * window and duplicate aggregation, so an error loop cannot flood the log files or the
 * IPC channel.
 */

import type { App } from 'vue';

export type ReportLevel = 'DEBUG' | 'INFO' | 'WARN' | 'ERROR';

export interface ReportEntry {
  level: ReportLevel;
  event: string;
  message?: string;
  fields?: Record<string, unknown>;
}

interface PendingEntry {
  entry: ReportEntry;
  count: number;
}

/** At most this many report entries leave the renderer per window. */
const RATE_WINDOW_MS = 10_000;
const MAX_ENTRIES_PER_WINDOW = 30;
/** Bounded aggregation table: protects memory if errors are all distinct. */
const MAX_PENDING_KEYS = 50;
/** Aggregated entries are flushed at most this often. */
const FLUSH_DELAY_MS = 1_000;
const MAX_MESSAGE_LENGTH = 300;
const MAX_STACK_LENGTH = 2000;

let installed = false;
let windowStartedAt = Date.now();
let entriesThisWindow = 0;
let droppedEntries = 0;
let flushTimer: ReturnType<typeof setTimeout> | null = null;
const pending = new Map<string, PendingEntry>();
let contextProvider: (() => Record<string, unknown>) | null = null;

function desktopApi(): any {
  return typeof window === 'undefined' ? undefined : (window as any).desktopAPI;
}

/** True only in the Electron build, where the restricted logging channel exists. */
export function isDiagnosticsAvailable(): boolean {
  const api = desktopApi();
  return Boolean(api && typeof api.reportLog === 'function');
}

/** Registers extra context (route name, app version) attached to every report. */
export function setDiagnosticsContext(provider: () => Record<string, unknown>): void {
  contextProvider = provider;
}

function safeMessage(value: unknown): string {
  try {
    if (value instanceof Error) return String(value.message ?? value.name ?? 'Error').slice(0, MAX_MESSAGE_LENGTH);
    if (typeof value === 'string') return value.slice(0, MAX_MESSAGE_LENGTH);
    if (value === null || value === undefined) return '';
    return String(value).slice(0, MAX_MESSAGE_LENGTH);
  } catch {
    return '';
  }
}

function errorFields(value: unknown): Record<string, unknown> {
  const fields: Record<string, unknown> = {};
  try {
    if (value instanceof Error) {
      fields.error_type = value.name || 'Error';
      fields.error_message = safeMessage(value.message);
      if (typeof value.stack === 'string') fields.stack = value.stack.slice(0, MAX_STACK_LENGTH);
    } else {
      fields.error_type = typeof value;
      fields.error_message = safeMessage(value);
    }
  } catch {
    /* ignore */
  }
  return fields;
}

function scheduleFlush(): void {
  if (flushTimer !== null) return;
  flushTimer = setTimeout(() => {
    flushTimer = null;
    void flushReports();
  }, FLUSH_DELAY_MS);
}

function withinRateWindow(): boolean {
  const now = Date.now();
  if (now - windowStartedAt >= RATE_WINDOW_MS) {
    windowStartedAt = now;
    entriesThisWindow = 0;
  }
  if (entriesThisWindow >= MAX_ENTRIES_PER_WINDOW) {
    droppedEntries += 1;
    return false;
  }
  entriesThisWindow += 1;
  return true;
}

/**
 * Queues one diagnostic report. Never throws and never blocks the caller; when the
 * channel is unavailable the call is a silent no-op (browser build).
 */
export function report(level: ReportLevel, event: string, message = '', fields: Record<string, unknown> = {}): void {
  try {
    if (!isDiagnosticsAvailable()) return;
    if (!withinRateWindow()) return;

    const key = `${level}|${event}|${message}`;
    const existing = pending.get(key);
    if (existing) {
      existing.count += 1;
      return;
    }
    if (pending.size >= MAX_PENDING_KEYS) {
      // Aggregation table full: flush what we have instead of growing unbounded.
      void flushReports();
    }
    pending.set(key, {
      entry: {
        level,
        event: event.slice(0, 120),
        message,
        fields: { ...fields, ...(contextProvider ? contextProvider() : {}) },
      },
      count: 1,
    });
    scheduleFlush();
  } catch {
    /* diagnostics must never break the app */
  }
}

/** Sends every queued report in one bounded IPC call. Never throws. */
export async function flushReports(): Promise<void> {
  try {
    if (pending.size === 0) return;
    const api = desktopApi();
    if (!api || typeof api.reportLog !== 'function') {
      pending.clear();
      return;
    }
    const batch: ReportEntry[] = [];
    for (const item of pending.values()) {
      const fields = { ...(item.entry.fields ?? {}) };
      if (item.count > 1) fields.occurrences = item.count;
      batch.push({ ...item.entry, fields });
    }
    pending.clear();

    try {
      await api.reportLog(batch);
    } catch {
      // A failed report is dropped on purpose: retrying could loop forever.
    }

    if (droppedEntries > 0) {
      const dropped = droppedEntries;
      droppedEntries = 0;
      try {
        await api.reportLog([
          {
            level: 'WARN',
            event: 'frontend.report_rate_limited',
            message: '前端错误上报超过限流阈值，超出部分已丢弃',
            fields: { dropped, window_ms: RATE_WINDOW_MS, limit: MAX_ENTRIES_PER_WINDOW },
          },
        ]);
      } catch {
        /* ignore */
      }
    }
  } catch {
    /* ignore */
  }
}

/**
 * Installs the two collectors the plan requires. Call once, right after the Vue app is
 * created and before mounting, so startup errors are captured too.
 */
export function installFrontendDiagnostics(app: App): void {
  if (installed) return;
  installed = true;
  if (!isDiagnosticsAvailable()) return;

  app.config.errorHandler = (err, _instance, info) => {
    // Vue's default handler would log this; keep developer visibility while reporting.
    try {
      console.error('[diagnostics] 捕获到 Vue 组件错误:', err);
    } catch {
      /* ignore */
    }
    report('ERROR', 'vue.error', safeMessage(err), {
      ...errorFields(err),
      info: String(info ?? '').slice(0, 200),
    });
  };

  window.addEventListener('unhandledrejection', (event: PromiseRejectionEvent) => {
    report('ERROR', 'promise.unhandled', safeMessage(event?.reason), errorFields(event?.reason));
  });

  // Flush on page hide so the last few reports are not lost when the window closes.
  window.addEventListener('pagehide', () => {
    void flushReports();
  });

  // Deliberately NOT intercepting console.* — the plan forbids global console collection.
}
