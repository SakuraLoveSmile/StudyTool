import { app, BrowserWindow, dialog, ipcMain, shell, session } from 'electron';
import path from 'path';
import fs from 'fs';
import crypto from 'crypto';
import http from 'http';
import { autoUpdater } from 'electron-updater';
import { CancellationToken } from 'electron-updater/out/types';
import { isPortAvailable } from './port-checker';
import { CredentialsStore, CredentialsUpdateRequest } from './credentials-store';
import { BackendManager } from './backend-manager';
import {
  ALL_SOURCES,
  DEFAULT_PAGE_SIZE,
  type LogQueryOptions,
  type LogQueryResult,
} from './log-store';
import {
  DiagnosticsLogger,
  getDiagnostics,
  initDiagnostics,
  type LogLevel,
} from './diagnostics';
import { writeArchiveAtomically } from './diagnostic-export';
import { LogWorkerService, type LogJobError } from './log-workers';
import {
  classifyConnectionTestTransportFailure,
  normalizeConnectionTestResult,
} from './connection-test';

const PORT = 18764;
const HOST = '127.0.0.1';
const BASE_URL = `http://${HOST}:${PORT}`;

let mainWindow: BrowserWindow | null = null;
let backendManager: BackendManager | null = null;
let isQuitting = false;
let isClosing = false;

type UpdateState = {
  state: 'idle' | 'checking' | 'available' | 'not-available' | 'downloading' | 'downloaded' | 'error';
  currentVersion: string;
  version?: string;
  releaseNotes?: string;
  percent?: number;
  error?: string;
};

let updateState: UpdateState = { state: 'idle', currentVersion: app.getVersion() };
let updateCancellation: CancellationToken | null = null;
let updateTimer: NodeJS.Timeout | null = null;

function publishUpdateState(next: Partial<UpdateState>): UpdateState {
  updateState = { ...updateState, ...next, currentVersion: app.getVersion() };
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('desktop:update-status', updateState);
  }
  return updateState;
}

function setupAutoUpdater(): void {
  if (!app.isPackaged) return;

  autoUpdater.autoDownload = false;
  autoUpdater.autoInstallOnAppQuit = false;
  autoUpdater.allowPrerelease = false;
  autoUpdater.allowDowngrade = false;

  autoUpdater.on('checking-for-update', () => publishUpdateState({ state: 'checking', error: undefined }));
  autoUpdater.on('update-available', (info) =>
    publishUpdateState({
      state: 'available',
      version: info.version,
      releaseNotes: typeof info.releaseNotes === 'string' ? info.releaseNotes : undefined,
      error: undefined,
    })
  );
  autoUpdater.on('update-not-available', () => publishUpdateState({ state: 'not-available', error: undefined }));
  autoUpdater.on('download-progress', (progress) =>
    publishUpdateState({ state: 'downloading', percent: Math.max(0, Math.min(100, progress.percent)) })
  );
  autoUpdater.on('update-downloaded', (info) =>
    publishUpdateState({
      state: 'downloaded',
      version: info.version,
      releaseNotes: typeof info.releaseNotes === 'string' ? info.releaseNotes : undefined,
      percent: 100,
      error: undefined,
    })
  );
  autoUpdater.on('error', (err) => {
    diagError('update.error', '自动更新出错', {
      error_type: err?.name,
      error_message: err?.message,
    });
    return publishUpdateState({ state: 'error', error: err.message || '更新服务不可用' });
  });
}

async function checkForUpdates(): Promise<UpdateState> {
  if (!app.isPackaged) {
    return publishUpdateState({ state: 'not-available', error: '开发模式不检查更新' });
  }
  if (updateState.state === 'checking' || updateState.state === 'downloading') return updateState;
  try {
    publishUpdateState({ state: 'checking', error: undefined });
    await autoUpdater.checkForUpdates();
  } catch (err: any) {
    diagError('update.check_failed', '检查更新失败', {
      error_type: err?.name,
      error_message: err?.message,
    });
    publishUpdateState({ state: 'error', error: err?.message || '检查更新失败' });
  }
  return updateState;
}

async function downloadUpdate(): Promise<UpdateState> {
  if (updateState.state !== 'available') return updateState;
  if (updateCancellation) return updateState;
  const cancellation = new CancellationToken();
  updateCancellation = cancellation;
  publishUpdateState({ state: 'downloading', percent: 0, error: undefined });
  try {
    await autoUpdater.downloadUpdate(cancellation);
  } catch (err: any) {
    if (!cancellation.cancelled) {
      diagError('update.download_failed', '下载更新失败', {
        error_type: err?.name,
        error_message: err?.message,
      });
      publishUpdateState({ state: 'error', error: err?.message || '下载更新失败' });
    }
  } finally {
    cancellation.dispose();
    if (updateCancellation === cancellation) updateCancellation = null;
  }
  return updateState;
}

function cancelUpdateDownload(): UpdateState {
  updateCancellation?.cancel();
  updateCancellation = null;
  return publishUpdateState({ state: 'available', percent: undefined, error: undefined });
}

async function installDownloadedUpdate(): Promise<UpdateState> {
  if (updateState.state !== 'downloaded') return updateState;
  const leaseId = crypto.randomUUID();
  let leaseAcquired = false;
  try {
    const status = await backendManager!.getStatus();
    if (!status.idle || status.active_tasks > 0) {
      return publishUpdateState({ state: 'error', error: '当前有任务正在运行，请稍后再安装更新' });
    }
    const lease = await makeBackendRequest('POST', '/api/desktop/maintenance/acquire', {
      lease_id: leaseId,
      operation: '安装软件更新',
      is_exit: true,
    });
    if (lease.status !== 200) {
      return publishUpdateState({ state: 'error', error: lease.data?.detail || '系统繁忙，无法安装更新' });
    }
    leaseAcquired = true;
    const stopped = await backendManager!.drainAndStop(leaseId, 30000);
    if (!stopped) {
      await makeBackendRequest('POST', '/api/desktop/maintenance/release', { lease_id: leaseId });
      return publishUpdateState({ state: 'error', error: '后台服务未确认退出，更新安装已中止' });
    }
    isQuitting = true;
    autoUpdater.quitAndInstall(false, true);
    return updateState;
  } catch (err: any) {
    if (leaseAcquired) {
      const restarted = await backendManager!.start();
      if (restarted) {
        try {
          await makeBackendRequest('POST', '/api/desktop/maintenance/release', { lease_id: leaseId });
        } catch {
          // The restarted backend will clear the lease on the next process exit.
        }
      }
    }
    return publishUpdateState({ state: 'error', error: err?.message || '安装更新失败' });
  }
}

// 1. Single instance lock: Focus existing window on secondary launch
const gotTheLock = app.requestSingleInstanceLock();
if (!gotTheLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });
}

// 2. Generate random 32-byte instance token for this launch
const instanceToken = crypto.randomBytes(32).toString('hex');
const credentialsStore = new CredentialsStore();
backendManager = new BackendManager(PORT, HOST, instanceToken, credentialsStore, app.isPackaged, {
  onEvent: (event, level, message, fields) => {
    // Resolved lazily: the logger is initialized immediately after this and the hook is
    // only ever invoked from process lifecycle callbacks.
    getDiagnostics()?.log(level, event, message, fields ?? {});
  },
});

// 2b. Diagnostics. The persistent log is initialized HERE — before the backend is
// spawned — so a startup failure still leaves evidence on disk. One session id is minted
// per launch and shared by the desktop / backend / frontend log streams.
const SESSION_ID =
  (process.env.RAG_SESSION_ID || '').trim() || `sess-${crypto.randomBytes(4).toString('hex')}`;
process.env.RAG_SESSION_ID = SESSION_ID;
process.env.RAG_APP_VERSION = app.getVersion();

const LOG_DIR = backendManager.getLogDir();
const desktopLogger: DiagnosticsLogger = initDiagnostics({
  dir: LOG_DIR,
  source: 'desktop',
  sessionId: SESSION_ID,
  appVersion: app.getVersion(),
});
/** Frontend reports arrive over restricted IPC and land in their own file. */
const frontendLogger: DiagnosticsLogger = new DiagnosticsLogger({
  dir: LOG_DIR,
  source: 'frontend',
  sessionId: SESSION_ID,
  appVersion: app.getVersion(),
  maxRecordsPerSecond: 50,
  aggregationWindowMs: 3000,
});

const DEFAULT_RANGE_MS = 24 * 60 * 60 * 1000;
const EXPORT_MAX_RANGE_MS = 7 * 24 * 60 * 60 * 1000;
const LOG_LEVELS = new Set(['DEBUG', 'INFO', 'WARN', 'ERROR']);
const LOG_ID_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;
/** Request ids are minted by this process; anything else from the renderer is ignored. */
const LOG_REQUEST_ID_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;
/** Upper bound on records a single frontend report call may carry. */
const FRONTEND_REPORT_MAX_BATCH = 100;

/** Set once the startup path has completed; used to keep failure handling honest. */
let appReady = false;

function diagInfo(event: string, message: string, fields: Record<string, unknown> = {}): void {
  try {
    desktopLogger.info(event, message, fields);
  } catch {
    /* diagnostics must never break startup */
  }
}

function diagWarn(event: string, message: string, fields: Record<string, unknown> = {}): void {
  try {
    desktopLogger.warn(event, message, fields);
  } catch {
    /* ignore */
  }
}

function diagError(event: string, message: string, fields: Record<string, unknown> = {}): void {
  try {
    desktopLogger.error(event, message, fields);
  } catch {
    /* ignore */
  }
}

function currentLogDir(): string {
  return getDiagnostics()?.getLogDir() || backendManager!.getLogDir();
}

/**
 * Log/export worker pool.
 *
 * Reading, parsing, scrubbing, sorting and archive building all happen off the main
 * thread, so a 48 MiB scan cannot stall IPC, repaint or navigation. Built *before* the
 * backend starts so the startup-failure export path can use it too — both channels are
 * plain worker threads and need neither the backend nor a renderer page.
 */
const logWorkers = new LogWorkerService({
  report: (event, message, fields) => {
    if (event === 'diagnostics.worker.timeout' || event === 'diagnostics.worker.crashed') {
      diagWarn(event, message, fields);
    } else {
      diagInfo(event, message, fields);
    }
  },
});

let requestCounter = 0;
/** Monotonic, per-launch request id: lets the renderer cancel and the main process match. */
function nextRequestId(prefix: string): string {
  requestCounter += 1;
  return `${prefix}-${Date.now().toString(36)}-${requestCounter.toString(36)}`;
}

/**
 * Normalizes an untrusted time range from the renderer: defaults to 24h, never extends
 * beyond 7 days, and never reaches into the future.
 */
function normalizeRange(params: unknown): { from: number; to: number } {
  const source = (params ?? {}) as Record<string, unknown>;
  const now = Date.now();
  let to = Number(source.to);
  if (!Number.isFinite(to) || to <= 0) to = now;
  if (to > now + 60_000) to = now;
  let from = Number(source.from);
  if (!Number.isFinite(from) || from <= 0 || from > to) from = to - DEFAULT_RANGE_MS;
  if (to - from > EXPORT_MAX_RANGE_MS) from = to - EXPORT_MAX_RANGE_MS;
  return { from: Math.floor(from), to: Math.floor(to) };
}

function logMeta(): Record<string, unknown> {
  return {
    ok: true,
    sessionId: SESSION_ID,
    appVersion: app.getVersion(),
    platform: process.platform,
    arch: process.arch,
    logDir: currentLogDir(),
    sources: ALL_SOURCES,
    levels: [...LOG_LEVELS],
    pageSize: DEFAULT_PAGE_SIZE,
    defaultRangeMs: DEFAULT_RANGE_MS,
    maxRangeMs: EXPORT_MAX_RANGE_MS,
  };
}

/**
 * A failed viewer query still answers with the full success shape, so the renderer has
 * one code path for rendering and one field (`code`) for deciding what to say.
 */
function failedQueryResult(
  requestId: string,
  error: LogJobError,
  page: number,
  pageSize: number
): LogQueryResult {
  return {
    ok: false,
    entries: [],
    total: 0,
    page,
    pageSize,
    hasMore: false,
    scannedBytes: 0,
    scanTruncated: false,
    unavailable: [],
    skippedLines: 0,
    requestId,
    code: error.code,
    message: error.message,
    retryable: error.retryable,
    elapsedMs: error.elapsedMs,
  };
}

/**
 * Validates and bounds every viewer query parameter, then hands the actual reading,
 * parsing, scrubbing and sorting to the worker thread. The main process never touches
 * the log files on this path, which is what keeps the window responsive.
 */
async function handleQueryLogs(params: unknown): Promise<LogQueryResult> {
  const source = (params ?? {}) as Record<string, unknown>;
  const requestId =
    typeof source.requestId === 'string' && LOG_REQUEST_ID_PATTERN.test(source.requestId)
      ? source.requestId
      : nextRequestId('q');

  const sources = Array.isArray(source.sources)
    ? (source.sources as unknown[]).filter(
        (item): item is string => typeof item === 'string' && (ALL_SOURCES as string[]).includes(item)
      )
    : [];

  const levels = Array.isArray(source.levels)
    ? (source.levels as unknown[])
        .filter((item): item is string => typeof item === 'string' && LOG_LEVELS.has(item.toUpperCase()))
        .map((item) => item.toUpperCase())
    : [];

  const sessionId =
    typeof source.sessionId === 'string' && LOG_ID_PATTERN.test(source.sessionId)
      ? source.sessionId
      : undefined;
  const taskId =
    typeof source.taskId === 'string' && LOG_ID_PATTERN.test(source.taskId) ? source.taskId : undefined;
  const keyword =
    typeof source.keyword === 'string' && source.keyword.trim()
      ? source.keyword.trim().slice(0, 100)
      : undefined;

  const page = Number.isFinite(Number(source.page)) ? Math.max(1, Math.floor(Number(source.page))) : 1;
  const pageSize = Number.isFinite(Number(source.pageSize))
    ? Math.min(500, Math.max(1, Math.floor(Number(source.pageSize))))
    : DEFAULT_PAGE_SIZE;

  const range = normalizeRange(source);
  const options: LogQueryOptions = {
    dir: currentLogDir(),
    sources: sources.length ? sources : ALL_SOURCES,
    from: range.from,
    to: range.to,
    levels,
    sessionId,
    taskId,
    keyword,
    page,
    pageSize,
  };

  const outcome = await logWorkers.query(requestId, options);
  if (!outcome.ok) {
    // Cancelled and superseded requests are normal control flow, not incidents: the
    // renderer asked for them and must not see an error banner.
    if (outcome.error.code !== 'LOG_QUERY_CANCELED' && outcome.error.code !== 'LOG_QUERY_SUPERSEDED') {
      diagWarn('diagnostics.query.failed', '日志查询失败', {
        code: outcome.error.code,
        error_message: outcome.error.message,
        duration_ms: outcome.error.elapsedMs,
      });
    }
    return failedQueryResult(requestId, outcome.error, page, pageSize);
  }

  return {
    ...outcome.value,
    requestId,
    elapsedMs: outcome.elapsedMs,
  };
}

/**
 * Accepts a bounded batch of frontend diagnostics. Fields are whitelisted and scrubbed by
 * the frontend logger itself, so a compromised renderer cannot smuggle content into the
 * log files through this channel.
 */
function handleReportLog(entries: unknown): { ok: boolean; accepted: number; dropped: number } {
  if (!Array.isArray(entries)) return { ok: false, accepted: 0, dropped: 0 };
  const batch = entries.slice(0, FRONTEND_REPORT_MAX_BATCH);
  let accepted = 0;
  for (const raw of batch) {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) continue;
    const item = raw as Record<string, unknown>;
    const levelText = String(item.level ?? 'ERROR').toUpperCase();
    const level = (LOG_LEVELS.has(levelText) ? levelText : 'ERROR') as LogLevel;
    const event = typeof item.event === 'string' && item.event ? item.event.slice(0, 120) : 'frontend.unknown';
    const message = typeof item.message === 'string' ? item.message : '';
    const fields =
      item.fields && typeof item.fields === 'object' && !Array.isArray(item.fields)
        ? (item.fields as Record<string, unknown>)
        : {};
    frontendLogger.log(level, event, message, fields);
    accepted += 1;
  }
  return { ok: true, accepted, dropped: Math.max(0, entries.length - batch.length) };
}

/**
 * Builds and saves a diagnostic bundle.
 *
 * The heavy half (read + scrub + ZIP) runs in a worker thread with a 60 s budget, so a
 * huge or pathological log can neither freeze the window nor occupy the main thread.
 * The native save dialog stays here, and the file is still written atomically — a
 * timeout or a failure never leaves a half-written archive behind.
 *
 * Deliberately independent of the backend and of any renderer page, so it also works
 * from the startup-failure dialog.
 */
async function exportDiagnostics(range: {
  from: number;
  to: number;
}): Promise<{
  ok: boolean;
  canceled?: boolean;
  path?: string;
  entries?: number;
  truncated?: boolean;
  message?: string;
  code?: string;
  elapsedMs?: number;
}> {
  const logger = getDiagnostics();
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const saveOptions: Electron.SaveDialogOptions = {
    title: '导出诊断包',
    defaultPath: `rag-diagnostics-${stamp}.zip`,
    filters: [{ name: 'ZIP 压缩包', extensions: ['zip'] }],
  };

  const parent = mainWindow && !mainWindow.isDestroyed() ? mainWindow : null;
  const result = parent
    ? await dialog.showSaveDialog(parent, saveOptions)
    : await dialog.showSaveDialog(saveOptions);

  // A cancelled dialog must leave nothing behind — the archive is only built after the
  // destination is confirmed, and the write itself is atomic.
  if (result.canceled || !result.filePath) return { ok: false, canceled: true };

  const requestId = nextRequestId('e');
  const outcome = await logWorkers.exportArchive(requestId, {
    dir: currentLogDir(),
    appVersion: app.getVersion(),
    sessionId: SESSION_ID,
    from: range.from,
    to: range.to,
    memoryFallback: logger?.getMemoryEntries() ?? [],
  });

  if (!outcome.ok) {
    diagError('diagnostics.export.failed', '诊断包导出失败', {
      code: outcome.error.code,
      error_message: outcome.error.message,
      duration_ms: outcome.error.elapsedMs,
    });
    return {
      ok: false,
      code: outcome.error.code,
      message: outcome.error.message,
      elapsedMs: outcome.error.elapsedMs,
    };
  }

  try {
    // Structured clone delivers a plain Uint8Array; only the main process writes files.
    const buffer = Buffer.from(outcome.value.buffer as unknown as Uint8Array);
    writeArchiveAtomically(result.filePath, buffer);
    diagInfo('diagnostics.export.ok', '诊断包导出完成', {
      entries: outcome.value.entries,
      bytes: buffer.length,
      truncated: outcome.value.truncated,
      note: outcome.value.note,
    });
    return {
      ok: true,
      path: result.filePath,
      entries: outcome.value.entries,
      truncated: outcome.value.truncated,
      elapsedMs: outcome.elapsedMs,
    };
  } catch (err: any) {
    // writeArchiveAtomically removes its own temp file on failure; keep the contract
    // explicit here so a failure can never leave a partial archive at the target path.
    diagError('diagnostics.export.write_failed', '诊断包写入失败', {
      error_type: err?.name,
      error_message: err?.message,
    });
    return { ok: false, code: 'LOG_EXPORT_FAILED', message: err?.message || String(err) };
  }
}

/**
 * Native startup-failure dialog offering log access, bundle export and exit.
 * Deliberately independent of the backend and of any renderer page.
 */
async function showStartupFailureDialog(context: {
  title: string;
  message: string;
  detail: string;
}): Promise<void> {
  let extra = '';
  for (;;) {
    const options: Electron.MessageBoxSyncOptions = {
      type: 'error',
      title: context.title,
      message: context.message,
      detail: [context.detail, extra, `日志目录：${currentLogDir()}`, `会话编号：${SESSION_ID}`]
        .filter(Boolean)
        .join('\n'),
      buttons: ['打开日志目录', '导出诊断包', '退出'],
      defaultId: 0,
      cancelId: 2,
      noLink: true,
    };

    const parent = mainWindow && !mainWindow.isDestroyed() ? mainWindow : null;
    const choice = parent
      ? dialog.showMessageBoxSync(parent, options)
      : dialog.showMessageBoxSync(options);

    if (choice === 0) {
      try {
        await shell.openPath(currentLogDir());
      } catch {
        /* ignore */
      }
      continue;
    }
    if (choice === 1) {
      const result = await exportDiagnostics({
        from: Date.now() - DEFAULT_RANGE_MS,
        to: Date.now(),
      });
      extra = result.ok
        ? `诊断包已导出：${result.path}（${result.entries ?? 0} 条记录${result.truncated ? '，已截断' : ''}）`
        : result.canceled
          ? '已取消导出，未生成任何文件。'
          : `导出失败：${result.message ?? '未知错误'}`;
      continue;
    }
    return;
  }
}

// NOTE: main-process `uncaughtException` / `unhandledRejection` handlers are intentionally
// NOT installed. Registering them would swallow crashes that currently terminate the app,
// changing failure semantics. Renderer-side Vue errors and unhandled rejections are
// collected instead (frontend/src/utils/diagnostics.ts), which is what the plan requires.

function validateIpcSender(event: Electron.IpcMainInvokeEvent): void {
  if (!mainWindow || event.sender !== mainWindow.webContents || event.senderFrame?.parent !== null) {
    throw new Error('未授权的 IPC 调用');
  }
}

function makeBackendRequest(
  method: string,
  apiPath: string,
  body?: any
): Promise<{ status: number; data: any }> {
  return new Promise((resolve, reject) => {
    const postData = body ? JSON.stringify(body) : '';
    const req = http.request(
      `${BASE_URL}${apiPath}`,
      {
        method,
        headers: {
          'Content-Type': 'application/json',
          'X-RAG-Instance-Token': instanceToken,
          Host: `${HOST}:${PORT}`,
          Origin: BASE_URL,
          ...(body ? { 'Content-Length': Buffer.byteLength(postData) } : {}),
        },
        timeout: 15000,
      },
      (res) => {
        let raw = '';
        res.on('data', (chunk) => (raw += chunk));
        res.on('end', () => {
          try {
            const parsed = raw ? JSON.parse(raw) : null;
            resolve({ status: res.statusCode || 500, data: parsed });
          } catch {
            resolve({ status: res.statusCode || 500, data: raw });
          }
        });
      }
    );

    req.on('error', (err) => reject(err));
    if (postData) req.write(postData);
    req.end();
  });
}

async function handleGracefulClose() {
  const exitLeaseId = crypto.randomUUID();
  let leaseAcquired = false;

  try {
    // 1. Acquire exit lease atomically to block new work, but do not preempt existing maintenance
    try {
      const mRes = await makeBackendRequest('POST', '/api/desktop/maintenance/acquire', {
        lease_id: exitLeaseId,
        operation: '退出软件',
        is_exit: true,
      });
      if (mRes.status === 200) {
        leaseAcquired = true;
      } else {
        dialog.showMessageBoxSync(mainWindow || (undefined as any), {
          type: 'warning',
          buttons: ['确定'],
          title: '正在维护',
          message: '系统当前正在执行其他维护操作，无法立即退出。',
          detail: mRes.data?.detail || '请等待当前维护操作完成后重试。',
        });
        isClosing = false;
        return;
      }
    } catch (err: any) {
      console.error('[Main] Failed acquiring exit lease:', err);
      diagError('shutdown.lease_failed', '无法进入安全退出流程', {
        error_type: err?.name,
        error_message: err?.message,
      });
      if (mainWindow && !mainWindow.isDestroyed()) {
        dialog.showMessageBoxSync(mainWindow, {
          type: 'error',
          buttons: ['确定'],
          title: '无法退出',
          message: '无法进入安全退出流程。',
          detail: err?.message || '请稍后重试。',
        });
      }
      isClosing = false;
      return;
    }

    // 2. Check active background tasks and operations
    const status = await backendManager!.getStatus();
    if (!status.idle && status.active_tasks > 0) {
      const choice = dialog.showMessageBoxSync(mainWindow || (undefined as any), {
        type: 'warning',
        buttons: ['取消退出', '中止任务并退出'],
        defaultId: 0,
        title: '退出确认',
        message: `后台尚有 ${status.active_tasks} 个正在进行的任务或生成/批改操作。`,
        detail: '退出将安全中止正在进行的任务（不会自动重复扣费发起请求，重新打开后可手动重试）。是否确认退出？',
      });

      if (choice === 0) {
        // User cancelled exit: release exit lease and resume normal operations
        if (leaseAcquired) {
          try {
            await makeBackendRequest('POST', '/api/desktop/maintenance/release', { lease_id: exitLeaseId });
          } catch {
            // ignore
          }
        }
        isClosing = false;
        return;
      }
    }

    // 3. Attempt graceful shutdown with 30s timeout
    let stopped = await backendManager!.drainAndStop(exitLeaseId, 30000);
    while (!stopped) {
      const choice = dialog.showMessageBoxSync(mainWindow || (undefined as any), {
        type: 'warning',
        buttons: ['继续等待 (15秒)', '强制退出'],
        defaultId: 0,
        title: '服务正在停机',
        message: '后台服务尚未完全停止。',
        detail: '强制退出可能导致部分在途状态未能完整刷盘。',
      });

      if (choice === 1) {
        // Explicit choice by user to force kill
        const killed = await backendManager!.forceKill();
        if (!killed) {
          dialog.showMessageBoxSync(mainWindow || (undefined as any), {
            type: 'error',
            buttons: ['确定'],
            title: '无法退出',
            message: '后台进程仍未确认退出。',
            detail: '应用保持打开，请稍后重试并查看运行日志。',
          });
          isClosing = false;
          return;
        }
        stopped = true;
        break;
      } else {
        // Continue waiting 15s; do NOT auto force-kill on timeout!
        stopped = await backendManager!.waitForExit(15000);
      }
    }
  } catch (err: any) {
    console.error('[Main] Error during graceful close:', err);
    diagError('shutdown.graceful_failed', '安全退出流程失败', {
      error_type: err?.name,
      error_message: err?.message,
    });
    if (leaseAcquired) {
      try {
        await makeBackendRequest('POST', '/api/desktop/maintenance/release', { lease_id: exitLeaseId });
      } catch {
        // Keep the window open when safe shutdown cannot be confirmed.
      }
    }
    if (mainWindow && !mainWindow.isDestroyed()) {
      dialog.showMessageBoxSync(mainWindow, {
        type: 'error',
        buttons: ['确定'],
        title: '无法安全退出',
        message: '后台服务未确认安全退出，应用仍保持打开。',
        detail: err?.message || '请检查运行日志后重试。',
      });
    }
    isClosing = false;
    return;
  }

  isQuitting = true;
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.destroy();
  }
  app.quit();
}

async function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 1024,
    minHeight: 700,
    title: '专业课备考助手',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  // Intercept external links: Open in external system browser strictly for http/https
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('http://') || url.startsWith('https://')) {
      shell.openExternal(url);
    }
    return { action: 'deny' };
  });

  mainWindow.webContents.on('will-navigate', (event, url) => {
    if (!url.startsWith(BASE_URL)) {
      event.preventDefault();
      if (url.startsWith('http://') || url.startsWith('https://')) {
        shell.openExternal(url);
      }
    }
  });

  // Window close interception: SYNCHRONOUSLY preventDefault
  mainWindow.on('close', (e) => {
    if (isQuitting) {
      return;
    }
    e.preventDefault();

    if (isClosing) {
      return;
    }
    isClosing = true;

    handleGracefulClose();
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
    diagInfo('window.closed', '桌面窗口已关闭');
  });

  // Renderer-level failure evidence: without these a blank window leaves no trace.
  mainWindow.webContents.on('did-finish-load', () => {
    diagInfo('window.load.ok', '桌面窗口加载完成', { target: BASE_URL });
  });

  mainWindow.webContents.on('did-fail-load', (_event, errorCode, errorDescription, validatedURL) => {
    diagError('window.load.failed', '桌面窗口加载失败', {
      status_code: errorCode,
      error_message: errorDescription,
      target: validatedURL,
    });
  });

  mainWindow.webContents.on('render-process-gone', (_event, details) => {
    diagError('renderer.gone', '渲染进程退出', {
      reason: details?.reason,
      exit_code: details?.exitCode,
    });
  });

  // Load the application
  await mainWindow.loadURL(BASE_URL);
}

// 3. Application Lifecycle
app.whenReady().then(async () => {
  appReady = true;
  diagInfo('startup.app.ready', '桌面主进程就绪，开始启动流程', {
    app_version: app.getVersion(),
  });

  // Setup session filter: inject instance token ONLY into local backend requests from mainWindow
  session.defaultSession.webRequest.onBeforeSendHeaders((details, callback) => {
    if (mainWindow && details.webContentsId !== mainWindow.webContents.id) {
      callback({ cancel: false, requestHeaders: details.requestHeaders });
      return;
    }

    try {
      const url = new URL(details.url);
      if (
        (url.protocol === 'http:' || url.protocol === 'https:') &&
        (url.hostname === '127.0.0.1' || url.hostname === 'localhost') &&
        String(url.port) === String(PORT)
      ) {
        details.requestHeaders['X-RAG-Instance-Token'] = instanceToken;
      }
    } catch {
      // ignore
    }
    callback({ cancel: false, requestHeaders: details.requestHeaders });
  });

  // Check port availability
  let portAvailable = await isPortAvailable(PORT, HOST);
  while (!portAvailable) {
    diagWarn('startup.port.conflict', '端口已被占用，等待用户处理', { target: `${HOST}:${PORT}` });
    const choice = dialog.showMessageBoxSync({
      type: 'error',
      title: '端口占用冲突',
      message: `端口 ${PORT} 已被其他程序占用。`,
      detail: [
        `桌面版固定使用 http://${HOST}:${PORT} 保证安全来源与反馈组件正常工作。请关闭冲突程序后重试。`,
        `日志目录：${currentLogDir()}`,
      ].join('\n'),
      buttons: ['重试', '打开日志目录', '导出诊断包', '退出程序'],
      defaultId: 0,
      cancelId: 3,
      noLink: true,
    });

    if (choice === 1) {
      try {
        await shell.openPath(currentLogDir());
      } catch {
        /* ignore */
      }
      continue;
    }
    if (choice === 2) {
      await exportDiagnostics({ from: Date.now() - DEFAULT_RANGE_MS, to: Date.now() });
      continue;
    }
    if (choice === 3) {
      diagWarn('startup.aborted', '用户因端口占用选择退出');
      isQuitting = true;
      app.quit();
      return;
    }
    portAvailable = await isPortAvailable(PORT, HOST);
  }

  // Start backend process
  diagInfo('startup.backend.begin', '开始启动本地后台服务');
  let backendStarted = false;
  let backendStartupError = '';
  try {
    backendStarted = await backendManager!.start();
  } catch (err: any) {
    backendStartupError = err?.message || String(err);
    diagError('startup.backend.failed', '后台启动过程抛出异常', {
      error_type: err?.name,
      error_message: backendStartupError,
    });
  }
  if (!backendStarted) {
    const detail = backendStartupError || backendManager!.getStartupFailureDetail();
    diagError('startup.backend.failed', '后台服务未启动或未通过就绪验证', { detail });
    await showStartupFailureDialog({
      title: '后台启动失败',
      message: '无法启动本地后台服务或未通过就绪验证。',
      detail: `${detail}\n请修复日志中指出的问题后重新启动。`,
    });
    isQuitting = true;
    app.quit();
    return;
  }

  // Setup IPC Handlers
  setupIpcHandlers();
  setupAutoUpdater();

  // Create Window
  diagInfo('startup.window.begin', '开始创建桌面窗口');
  try {
    await createWindow();
  } catch (err: any) {
    diagError('startup.window.failed', '桌面窗口加载失败', {
      error_type: err?.name,
      error_message: err?.message || String(err),
    });
    await showStartupFailureDialog({
      title: '窗口启动失败',
      message: '本地后台已启动，但桌面窗口无法加载。',
      detail: err?.message || String(err),
    });
    await backendManager!.forceKill();
    isQuitting = true;
    app.quit();
    return;
  }

  diagInfo('startup.completed', '启动流程完成', { phase: 'ready', target: BASE_URL });

  if (app.isPackaged) {
    setTimeout(() => void checkForUpdates(), 1500);
    updateTimer = setInterval(() => void checkForUpdates(), 6 * 60 * 60 * 1000);
  }
});

function setupIpcHandlers() {
  ipcMain.handle('desktop:getUpdateStatus', (event) => {
    validateIpcSender(event);
    return updateState;
  });

  ipcMain.handle('desktop:checkForUpdates', async (event) => {
    validateIpcSender(event);
    return checkForUpdates();
  });

  ipcMain.handle('desktop:downloadUpdate', async (event) => {
    validateIpcSender(event);
    return downloadUpdate();
  });

  ipcMain.handle('desktop:cancelUpdate', (event) => {
    validateIpcSender(event);
    return cancelUpdateDownload();
  });

  ipcMain.handle('desktop:installUpdate', async (event) => {
    validateIpcSender(event);
    return installDownloadedUpdate();
  });

  // Credentials Status
  ipcMain.handle('desktop:getCredentialsStatus', async (event) => {
    validateIpcSender(event);
    let hasVectors = false;
    try {
      const sysStatus = await makeBackendRequest('GET', '/api/desktop/status');
      hasVectors = Boolean(sysStatus.data?.has_existing_embeddings);
    } catch {
      hasVectors = true; // safe fallback
    }
    return credentialsStore.getStatus(hasVectors);
  });

  // Save Credentials with rollback and maintenance lease
  ipcMain.handle('desktop:saveCredentials', async (event, creds: CredentialsUpdateRequest) => {
    validateIpcSender(event);
    const leaseId = crypto.randomUUID();

    // 1. Acquire maintenance lease
    try {
      const mRes = await makeBackendRequest('POST', '/api/desktop/maintenance/acquire', {
        lease_id: leaseId,
        operation: '修改模型配置',
      });
      if (mRes.status !== 200) {
        return { ok: false, message: mRes.data?.detail || '系统繁忙或正在维护，无法保存配置' };
      }
    } catch (err: any) {
      return { ok: false, message: `无法进入维护状态: ${err.message}` };
    }

    // 2. Validate embedding constraints
    try {
      const valRes = await makeBackendRequest('POST', '/api/desktop/config/validate', {
        new_embedding_base_url: creds.embedding_base_url,
        new_embedding_model: creds.embedding_model,
      });
      if (valRes.status !== 200) {
        await makeBackendRequest('POST', '/api/desktop/maintenance/release', { lease_id: leaseId });
        return { ok: false, message: valRes.data?.detail || '配置校验失败' };
      }
    } catch (err: any) {
      await makeBackendRequest('POST', '/api/desktop/maintenance/release', { lease_id: leaseId });
      return { ok: false, message: `配置校验连接异常: ${err.message}` };
    }

    // 3. Switch configuration with automatic rollback
    const switchRes = await backendManager!.switchConfigWithRollback(creds, leaseId);

    // Release maintenance
    try {
      await makeBackendRequest('POST', '/api/desktop/maintenance/release', { lease_id: leaseId });
    } catch {
      // ignore
    }

    return switchRes;
  });

  ipcMain.handle('desktop:clearCredentials', async (event) => {
    validateIpcSender(event);
    const leaseId = crypto.randomUUID();
    try {
      const mRes = await makeBackendRequest('POST', '/api/desktop/maintenance/acquire', {
        lease_id: leaseId,
        operation: '清除凭据',
      });
      if (mRes.status !== 200) {
        return { ok: false, message: mRes.data?.detail || '系统繁忙或正在维护，无法清除凭据' };
      }
    } catch (err: any) {
      return { ok: false, message: `无法进入维护状态: ${err.message}` };
    }

    const clearRes = await backendManager!.switchConfigWithRollback(null, leaseId);

    try {
      await makeBackendRequest('POST', '/api/desktop/maintenance/release', { lease_id: leaseId });
    } catch {
      // ignore
    }

    return clearRes;
  });

  ipcMain.handle('desktop:testConnection', async (event, serviceType, customConfig) => {
    validateIpcSender(event);
    const startedAt = Date.now();
    try {
      const res = await makeBackendRequest('POST', '/api/desktop/test-connection', {
        service_type: serviceType,
        base_url: customConfig?.base_url,
        model: customConfig?.model,
        api_key: customConfig?.api_key,
        baidu_ocr_url: customConfig?.baidu_ocr_url,
        baidu_ocr_token: customConfig?.baidu_ocr_token,
      });
      const outcome = normalizeConnectionTestResult(
        serviceType,
        res.status,
        res.data,
        Date.now() - startedAt
      );
      if (outcome.event === 'connection_test.invalid_response') {
        diagWarn(outcome.event, outcome.message, outcome.fields);
      } else if (outcome.event) {
        diagError(outcome.event, outcome.message, outcome.fields);
      }
      return outcome.result;
    } catch (err: any) {
      // Transport-level failure: the local backend itself could not be reached. This is a
      // different class of problem from the backend reaching the LLM and failing.
      const failure = classifyConnectionTestTransportFailure(
        serviceType,
        err,
        Date.now() - startedAt
      );
      diagError(failure.event, failure.message, failure.fields);
      return failure.result;
    }
  });

  ipcMain.handle('desktop:getSystemStatus', async (event) => {
    validateIpcSender(event);
    try {
      const res = await makeBackendRequest('GET', '/api/desktop/status');
      return res.data;
    } catch {
      return { idle: false, active_tasks: 1, maintenance: false };
    }
  });

  ipcMain.handle('desktop:reindex', async (event) => {
    validateIpcSender(event);
    const leaseId = crypto.randomUUID();
    try {
      const lease = await makeBackendRequest('POST', '/api/desktop/maintenance/acquire', {
        lease_id: leaseId,
        operation: '重建向量索引',
      });
      if (lease.status !== 200) {
        return { ok: false, message: lease.data?.detail || '系统繁忙，无法开始重建索引' };
      }
      const res = await makeBackendRequest('POST', '/api/desktop/reindex', { lease_id: leaseId });
      return res.data;
    } catch (err: any) {
      return { ok: false, message: `重建索引请求失败: ${err.message}` };
    } finally {
      try {
        await makeBackendRequest('POST', '/api/desktop/maintenance/release', { lease_id: leaseId });
      } catch {
        // Backend may already be restarting after a failed reindex.
      }
    }
  });

  // Backup & Restore Dialogs
  ipcMain.handle('desktop:selectBackupFile', async (event) => {
    validateIpcSender(event);
    if (!mainWindow) return null;
    const result = await dialog.showOpenDialog(mainWindow, {
      title: '选择恢复数据备份包',
      filters: [{ name: 'RAG 归档文件', extensions: ['tar.gz', 'tgz', 'gz'] }],
      properties: ['openFile'],
    });
    if (result.canceled || result.filePaths.length === 0) return null;
    return result.filePaths[0];
  });

  ipcMain.handle('desktop:selectSaveBackupLocation', async (event) => {
    validateIpcSender(event);
    if (!mainWindow) return null;
    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    const result = await dialog.showSaveDialog(mainWindow, {
      title: '选择备份导出保存位置',
      defaultPath: `rag-backup-${stamp}.tar.gz`,
      filters: [{ name: 'RAG 归档文件', extensions: ['tar.gz'] }],
    });
    if (result.canceled || !result.filePath) return null;
    return result.filePath;
  });

  // T3: Offline Backup (stopping backend before copying/archiving)
  ipcMain.handle('desktop:createBackup', async (event, targetFilePath?: string) => {
    validateIpcSender(event);
    const leaseId = crypto.randomUUID();

    // 1. Enter maintenance mode
    try {
      const mRes = await makeBackendRequest('POST', '/api/desktop/maintenance/acquire', {
        lease_id: leaseId,
        operation: '导出数据备份',
      });
      if (mRes.status !== 200) {
        return { ok: false, message: mRes.data?.detail || '系统繁忙，无法开始备份' };
      }
    } catch (err: any) {
      return { ok: false, message: `无法进入备份维护模式: ${err.message}` };
    }

    try {
      // 2. Stop backend completely for safe offline backup (30s timeout guard)
      const stopped = await backendManager!.drainAndStop(leaseId, 30000);
      if (!stopped) {
        await makeBackendRequest('POST', '/api/desktop/maintenance/release', { lease_id: leaseId });
        return { ok: false, message: '后台服务停机超时（30秒内未完全退出），备份已安全中止' };
      }

      // 3. Extract actual data directory and non-sensitive model parameters
      let creds: any = {};
      try {
        creds = credentialsStore.load();
      } catch {
        creds = {};
      }
      const dataDir = backendManager!.getDataDir();
      const outParam = targetFilePath || 'rag-backup.tar.gz';
      const toolArgs = ['--output', outParam, '--source', dataDir];
      if (creds.embedding_model) {
        toolArgs.push('--embedding-model', creds.embedding_model);
      }
      if (creds.embedding_base_url) {
        toolArgs.push('--embedding-base-url', creds.embedding_base_url);
      }
      if (creds.llm_model) {
        toolArgs.push('--llm-model', creds.llm_model);
      }

      const result = await backendManager!.runOfflineTool('backup', toolArgs);

      // 4. Restart backend
      await backendManager!.start();

      // 5. Release lease
      try {
        await makeBackendRequest('POST', '/api/desktop/maintenance/release', { lease_id: leaseId });
      } catch {
        // ignore
      }

      return {
        ok: true,
        archive_path: targetFilePath,
        message: '备份创建成功',
      };
    } catch (err: any) {
      // Ensure backend is restarted and lease released
      await backendManager!.start();
      try {
        await makeBackendRequest('POST', '/api/desktop/maintenance/release', { lease_id: leaseId });
      } catch {
        // ignore
      }
      return { ok: false, message: `备份执行失败: ${err.message}` };
    }
  });

  // T3: Offline Restore (verifies compatibility, stops backend, restores, restarts)
  ipcMain.handle('desktop:restoreBackup', async (event, archivePath: string) => {
    validateIpcSender(event);
    if (!archivePath) {
      return { ok: false, message: '未指定备份文件路径' };
    }

    const leaseId = crypto.randomUUID();

    // 1. Enter maintenance mode
    try {
      const mRes = await makeBackendRequest('POST', '/api/desktop/maintenance/acquire', {
        lease_id: leaseId,
        operation: '恢复数据备份',
      });
      if (mRes.status !== 200) {
        return { ok: false, message: mRes.data?.detail || '系统繁忙，无法开始恢复' };
      }
    } catch (err: any) {
      return { ok: false, message: `无法进入恢复维护模式: ${err.message}` };
    }

    try {
      // 2. Stop backend completely for safe offline restore (30s timeout guard)
      const stopped = await backendManager!.drainAndStop(leaseId, 30000);
      if (!stopped) {
        await makeBackendRequest('POST', '/api/desktop/maintenance/release', { lease_id: leaseId });
        return { ok: false, message: '后台服务停机超时（30秒内未完全退出），恢复已安全中止' };
      }

      // 3. Extract actual data directory and expected model / base url
      let creds: any = {};
      try {
        creds = credentialsStore.load();
      } catch {
        creds = {};
      }

      const res = await backendManager!.executeRestoreWithRollback(archivePath, creds);

      // 4. Release lease
      try {
        await makeBackendRequest('POST', '/api/desktop/maintenance/release', { lease_id: leaseId });
      } catch {
        // ignore
      }

      // 5. Reload renderer on success
      if (res.ok) {
        mainWindow?.reload();
      }

      return res;
    } catch (err: any) {
      // Ensure backend is restarted if failed
      await backendManager!.start();
      try {
        await makeBackendRequest('POST', '/api/desktop/maintenance/release', { lease_id: leaseId });
      } catch {
        // ignore
      }
      return { ok: false, message: `恢复失败: ${err.message}` };
    }
  });

  // External URLs
  ipcMain.handle('desktop:openExternal', (event, url: string) => {
    validateIpcSender(event);
    if (url && (url.startsWith('http://') || url.startsWith('https://'))) {
      shell.openExternal(url);
    }
  });

  ipcMain.handle('desktop:openLogDirectory', (event) => {
    validateIpcSender(event);
    const logDir = currentLogDir();
    fs.mkdirSync(logDir, { recursive: true });
    shell.openPath(logDir);
  });

  // --- Test-build diagnostics surface (restricted IPC) --------------------------------
  // There is deliberately no public HTTP log endpoint and no IPC that reads an arbitrary
  // path: the renderer can only query the three known streams, and export always goes
  // through a native save dialog owned by the main process.
  ipcMain.handle('desktop:getLogMeta', (event) => {
    validateIpcSender(event);
    return logMeta();
  });

  ipcMain.handle('desktop:queryLogs', (event, params: unknown) => {
    validateIpcSender(event);
    return handleQueryLogs(params);
  });

  // Cancellation is a first-class part of the query contract: the renderer discards a
  // superseded response, but only the main process can actually stop the worker.
  ipcMain.handle('desktop:cancelLogQuery', (event, requestId: unknown) => {
    validateIpcSender(event);
    if (typeof requestId !== 'string' || !LOG_REQUEST_ID_PATTERN.test(requestId)) {
      return { ok: false, canceled: false };
    }
    const canceled = logWorkers.cancelQuery(requestId);
    if (canceled) {
      diagInfo('diagnostics.query.canceled', '日志查询已取消', { request_id: requestId });
    }
    return { ok: true, canceled };
  });

  ipcMain.handle('desktop:reportLog', (event, entries: unknown) => {
    validateIpcSender(event);
    return handleReportLog(entries);
  });

  ipcMain.handle('desktop:exportDiagnostics', async (event, params: unknown) => {
    validateIpcSender(event);
    return exportDiagnostics(normalizeRange(params));
  });
}

app.on('window-all-closed', () => {
  if (isQuitting) {
    app.quit();
  } else if (process.platform !== 'darwin') {
    handleGracefulClose();
  }
});

app.on('before-quit', (e) => {
  if (!isQuitting) {
    e.preventDefault();
    handleGracefulClose();
  }
});

app.on('will-quit', () => {
  // Release the worker threads first: an abandoned scan must not outlive the app.
  try {
    logWorkers.shutdown();
  } catch {
    /* ignore */
  }
  // Flush aggregated duplicates and close both writers so nothing is lost on exit.
  try {
    getDiagnostics()?.close();
  } catch {
    /* ignore */
  }
  try {
    frontendLogger.close();
  } catch {
    /* ignore */
  }
});
