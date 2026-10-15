import { ref, computed, onBeforeUnmount } from 'vue';
import axios from 'axios';
import { api } from '../api/client';

/** PP-OCRv6 异步 jobs 识别接口地址：新配置（未保存过任何地址）时的默认值。 */
export const PP_OCR_V6_JOBS_URL = 'https://paddleocr.aistudio-app.com/api/v2/ocr/jobs';

/**
 * 是否为 PP-OCRv6 jobs 地址。忽略首尾空白、末尾斜杠与查询串，避免同一地址因写法差异
 * 被判成旧地址。
 */
export function isOcrJobsUrl(url: string | null | undefined): boolean {
  const normalized = String(url ?? '').trim();
  if (!normalized) return false;
  const withoutQuery = normalized.split(/[?#]/)[0].replace(/\/+$/, '');
  return withoutQuery.endsWith('/api/v2/ocr/jobs');
}

/**
 * 已保存的旧版（同步预测接口）地址：需要提示用户更新，但绝不静默改写已保存值。
 */
export function isLegacyOcrApiUrl(url: string | null | undefined): boolean {
  return Boolean(String(url ?? '').trim()) && !isOcrJobsUrl(url);
}

export interface DesktopCredentialsStatus {
  llm: {
    base_url: string;
    model: string;
    configured: boolean;
  };
  embedding: {
    base_url: string;
    model: string;
    configured: boolean;
    locked?: boolean;
  };
  baidu_ocr: {
    api_url: string;
    configured: boolean;
  };
  ocr?: {
    mode: string;
    provider: string;
    base_url: string;
    model: string;
    configured: boolean;
    message: string;
    source: string;
  };
}

export interface DesktopSystemStatus {
  idle: boolean;
  idle_reason?: string;
  active_tasks: number;
  has_existing_embeddings: boolean;
  desktop_mode: boolean;
  vector_locked?: boolean;
  vector_lock_reason?: string;
}

export interface DesktopUpdateStatus {
  state: 'idle' | 'checking' | 'available' | 'not-available' | 'downloading' | 'downloaded' | 'error';
  currentVersion: string;
  version?: string;
  releaseNotes?: string;
  percent?: number;
  error?: string;
}

export interface DesktopLogMeta {
  ok: boolean;
  sessionId: string;
  appVersion: string;
  platform: string;
  arch: string;
  logDir: string;
  sources: string[];
  levels: string[];
  pageSize: number;
  defaultRangeMs: number;
  maxRangeMs: number;
}

export interface DesktopLogEntry {
  timestamp: string;
  timestamp_ms: number;
  level: string;
  source: string;
  event: string;
  message: string;
  session_id: string | null;
  task_id?: string;
  /**
   * 主进程为每条成功读取的记录分配的稳定标识（成功结果里每条都有）。
   * 仍按可选处理：旧版主进程或降级路径可能没有该字段。
   */
  record_id?: string;
  [key: string]: unknown;
}

export interface DesktopLogQuery {
  /**
   * 渲染进程提议的请求编号（主进程只接受 /^[A-Za-z0-9_-]{1,64}$/，不合法时自行生成）。
   * 预先提供编号，查询在途时才可能用 cancelLogQuery 精确取消它。
   */
  requestId?: string;
  from: number;
  to: number;
  levels?: string[];
  sources?: string[];
  sessionId?: string;
  taskId?: string;
  keyword?: string;
  page?: number;
  pageSize?: number;
}

export interface DesktopLogQueryResult {
  ok: boolean;
  /** 本次请求编号；失败结果里同样存在。 */
  requestId?: string;
  /** 失败时为空数组：失败结果仍是完整结构。 */
  entries: DesktopLogEntry[];
  total: number;
  page: number;
  pageSize: number;
  hasMore: boolean;
  scannedBytes: number;
  scanTruncated: boolean;
  unavailable: string[];
  /** 超大/损坏行被跳过的条数。 */
  skippedLines?: number;
  elapsedMs?: number;
  /** 失败时的错误码，如 LOG_QUERY_TIMEOUT / LOG_QUERY_CANCELED / LOG_QUERY_SUPERSEDED。 */
  code?: string;
  /** 失败时的中文说明。 */
  message?: string;
  /** 失败是否可重试（超时、被取代、Worker 崩溃为 true）。 */
  retryable?: boolean;
}

export interface DesktopDiagnosticExportResult {
  ok: boolean;
  canceled?: boolean;
  path?: string;
  entries?: number;
  truncated?: boolean;
  /** 诊断包内容不完整或部分来自内存（读取错误/截断/待写丢弃等，详情见 MANIFEST.json）。 */
  incomplete?: boolean;
  code?: string;
  elapsedMs?: number;
  message?: string;
}

/** 反馈附件自动采集：与 desktop/src/feedback-logs.ts 的冻结契约逐字一致。 */
export type FeedbackLogSource = 'desktop' | 'backend' | 'frontend';
export type FeedbackLogFilename =
  | 'studytool-desktop.jsonl'
  | 'studytool-backend.jsonl'
  | 'studytool-frontend.jsonl';
export type FeedbackLogsResult =
  | {
      ok: true;
      files: Array<{ filename: FeedbackLogFilename; text: string }>;
      truncated: boolean;
      unavailable: FeedbackLogSource[];
      skippedLines: number;
    }
  | {
      ok: false;
      code:
        | 'LOG_FEEDBACK_BUSY'
        | 'LOG_FEEDBACK_TIMEOUT'
        | 'LOG_FEEDBACK_CANCELED'
        | 'LOG_FEEDBACK_UNAVAILABLE'
        | 'LOG_FEEDBACK_FAILED';
      message: string;
    };

/** 外观偏好：与主进程独立偏好文件同源（useTheme.ts 共用该类型）。 */
export type ThemePreference = 'system' | 'light' | 'dark';
export type EffectiveTheme = 'light' | 'dark';
export interface DesktopThemePayload {
  preference: ThemePreference;
  effective: EffectiveTheme;
}
export interface DesktopThemeSetResult extends DesktopThemePayload {
  ok: boolean;
  message?: string;
}

declare global {
  interface Window {
    desktopAPI?: {
      isDesktop: boolean;
      getCredentialsStatus(): Promise<DesktopCredentialsStatus>;
      saveCredentials(data: any): Promise<{ ok: boolean; message: string }>;
      clearCredentials(): Promise<{ ok: boolean }>;
      testConnection(serviceType: string, customConfig?: any): Promise<any>;
      getSystemStatus(): Promise<DesktopSystemStatus>;
      reindex?(): Promise<{ ok: boolean; message: string; reindexed_count?: number }>;
      getUpdateStatus?(): Promise<DesktopUpdateStatus>;
      checkForUpdates?(): Promise<DesktopUpdateStatus>;
      downloadUpdate?(): Promise<DesktopUpdateStatus>;
      cancelUpdate?(): Promise<DesktopUpdateStatus>;
      installUpdate?(): Promise<DesktopUpdateStatus>;
      onUpdateStatus?(callback: (status: DesktopUpdateStatus) => void): () => void;
      selectBackupFile(): Promise<string | null>;
      selectSaveBackupLocation(): Promise<string | null>;
      createBackup(destinationDir?: string): Promise<any>;
      restoreBackup(archivePath: string): Promise<any>;
      openExternal(url: string): void;
      openLogDirectory(): Promise<{ ok: boolean; message?: string }>;
      /** Diagnostics (test build). Read-only: there is no delete/clear channel. */
      getLogMeta?(): Promise<DesktopLogMeta>;
      queryLogs?(params: DesktopLogQuery): Promise<DesktopLogQueryResult>;
      /** 取消在途查询；未暴露该接口时调用方必须忽略（可选链调用）。 */
      cancelLogQuery?(requestId: string): Promise<{ ok: boolean; canceled: boolean }>;
      reportLog?(entries: unknown[]): Promise<{ ok: boolean; accepted: number; dropped: number }>;
      exportDiagnostics?(params: { from: number; to: number }): Promise<DesktopDiagnosticExportResult>;
      /** 反馈附件自动采集：无参数受限 IPC；旧版桌面端可能未暴露（可选链调用）。 */
      getFeedbackLogs?(): Promise<FeedbackLogsResult>;
      /** 外观偏好：preload 在首屏前同步注入的初始值；读写均走受限 IPC。 */
      initialTheme?: DesktopThemePayload;
      getThemePreference?(): Promise<DesktopThemePayload>;
      setThemePreference?(preference: ThemePreference): Promise<DesktopThemeSetResult>;
      onThemeChanged?(callback: (payload: DesktopThemePayload) => void): () => void;
    };
  }
}

export function useDesktop() {
  const isDesktop = computed(() => Boolean(typeof window !== 'undefined' && window.desktopAPI?.isDesktop));
  const loading = ref(false);
  const status = ref<DesktopSystemStatus | null>(null);
  const credentialsStatus = ref<DesktopCredentialsStatus | null>(null);
  const errorMessage = ref('');
  const updateStatus = ref<DesktopUpdateStatus>({ state: 'idle', currentVersion: 'web' });
  let removeUpdateListener: (() => void) | undefined;

  const fetchStatus = async () => {
    loading.value = true;
    errorMessage.value = '';
    try {
      if (isDesktop.value && window.desktopAPI) {
        credentialsStatus.value = await window.desktopAPI.getCredentialsStatus();
        const sysStatus = await window.desktopAPI.getSystemStatus();
        status.value = sysStatus;
        if (window.desktopAPI.getUpdateStatus) {
          updateStatus.value = await window.desktopAPI.getUpdateStatus();
        }
        if (credentialsStatus.value?.embedding) {
          credentialsStatus.value.embedding.locked = sysStatus.has_existing_embeddings || Boolean(sysStatus.vector_locked);
        }
      } else {
        // Web / dev fallback via HTTP
        const res = await axios.get('/api/desktop/status');
        const data = res.data;
        status.value = {
          idle: data.idle,
          idle_reason: data.idle_reason,
          active_tasks: data.active_tasks,
          has_existing_embeddings: data.has_existing_embeddings,
          desktop_mode: data.desktop_mode,
          vector_locked: data.vector_locked,
          vector_lock_reason: data.vector_lock_reason,
        };
        credentialsStatus.value = {
          llm: {
            base_url: data.llm_base_url,
            model: data.llm_model,
            configured: data.llm_configured,
          },
          embedding: {
            base_url: data.embedding_base_url,
            model: data.embedding_model,
            configured: data.embedding_configured,
            locked: data.has_existing_embeddings || Boolean(data.vector_locked),
          },
          baidu_ocr: {
            api_url: data.baidu_ocr_api_url || '',
            configured: data.baidu_ocr_configured,
          },
          ocr: data.ocr,
        };
      }
    } catch (err: any) {
      if (!isDesktop.value) credentialsStatus.value = null;
      errorMessage.value = err.message || '获取配置状态失败';
    } finally {
      loading.value = false;
    }
  };

  const saveCredentials = async (creds: any) => {
    loading.value = true;
    errorMessage.value = '';
    try {
      if (isDesktop.value && window.desktopAPI) {
        const res = await window.desktopAPI.saveCredentials(creds);
        if (!res.ok) {
          throw new Error(res.message || '保存凭据失败');
        }
        await fetchStatus();
        return res;
      } else {
        throw new Error('网页使用服务端统一配置，请在服务端修改配置后重启服务。');
      }
    } catch (err: any) {
      errorMessage.value = err.response?.data?.detail || err.message;
      throw err;
    } finally {
      loading.value = false;
    }
  };

  const testConnection = async (serviceType: string, customConfig?: any) => {
    if (isDesktop.value && window.desktopAPI) {
      return await window.desktopAPI.testConnection(serviceType, customConfig);
    } else {
      try {
        const res = await api.post('/desktop/test-connection', {
          service_type: serviceType,
          ...customConfig,
        }, { timeout: 180000 });
        return res.data;
      } catch (err: any) {
        // Surface the backend's own reason (e.g. HTTPException `detail`) instead of the
        // generic axios "Request failed with status code 400".
        const detail = err?.response?.data?.detail;
        return {
          ok: false,
          service: serviceType,
          error_type: 'backend_error',
          message: typeof detail === 'string' ? detail : detail?.message || err.message || '测试请求失败',
        };
      }
    }
  };

  const createBackup = async () => {
    loading.value = true;
    errorMessage.value = '';
    try {
      let destPath: string | undefined = undefined;
      if (isDesktop.value && window.desktopAPI) {
        const selected = await window.desktopAPI.selectSaveBackupLocation();
        if (!selected) return null;
        destPath = selected;
        const res = await window.desktopAPI.createBackup(destPath);
        return res;
      } else {
        const res = await axios.post('/api/desktop/backup/create', {});
        return res.data;
      }
    } catch (err: any) {
      errorMessage.value = err.response?.data?.detail || err.message;
      throw err;
    } finally {
      loading.value = false;
    }
  };

  const restoreBackup = async () => {
    loading.value = true;
    errorMessage.value = '';
    try {
      if (isDesktop.value && window.desktopAPI) {
        const selected = await window.desktopAPI.selectBackupFile();
        if (!selected) return null;
        const res = await window.desktopAPI.restoreBackup(selected);
        return res;
      } else {
        throw new Error('Web 模式暂不支持直接触发本地恢复，请使用桌面端运行');
      }
    } catch (err: any) {
      errorMessage.value = err.response?.data?.detail || err.message;
      throw err;
    } finally {
      loading.value = false;
    }
  };

  const openLogDirectory = async () => {
    if (!isDesktop.value || !window.desktopAPI) {
      return { ok: false, message: '当前环境不支持打开日志目录' };
    }
    try {
      const res = await window.desktopAPI.openLogDirectory();
      if (res && res.ok === false) {
        errorMessage.value = res.message || '无法打开日志目录';
      }
      return res ?? { ok: true };
    } catch (err: any) {
      const message = err?.message || '无法打开日志目录';
      errorMessage.value = message;
      return { ok: false, message };
    }
  };

  const reindex = async () => {
    loading.value = true;
    errorMessage.value = '';
    try {
      if (isDesktop.value && window.desktopAPI?.reindex) {
        const res = await window.desktopAPI.reindex();
        await fetchStatus();
        return res;
      } else {
        const res = await axios.post('/api/desktop/reindex');
        await fetchStatus();
        return res.data;
      }
    } catch (err: any) {
      const msg = err.response?.data?.detail || err.message || '重建索引失败';
      errorMessage.value = msg;
      return { ok: false, message: msg };
    } finally {
      loading.value = false;
    }
  };

  const checkForUpdates = async () => {
    if (!isDesktop.value || !window.desktopAPI?.checkForUpdates) {
      return updateStatus.value;
    }
    updateStatus.value = await window.desktopAPI.checkForUpdates();
    return updateStatus.value;
  };

  const downloadUpdate = async () => {
    if (!isDesktop.value || !window.desktopAPI?.downloadUpdate) return updateStatus.value;
    updateStatus.value = await window.desktopAPI.downloadUpdate();
    return updateStatus.value;
  };

  const cancelUpdate = async () => {
    if (!isDesktop.value || !window.desktopAPI?.cancelUpdate) return updateStatus.value;
    updateStatus.value = await window.desktopAPI.cancelUpdate();
    return updateStatus.value;
  };

  const installUpdate = async () => {
    if (!isDesktop.value || !window.desktopAPI?.installUpdate) return updateStatus.value;
    updateStatus.value = await window.desktopAPI.installUpdate();
    return updateStatus.value;
  };

  if (typeof window !== 'undefined' && window.desktopAPI?.onUpdateStatus) {
    removeUpdateListener = window.desktopAPI.onUpdateStatus((next) => {
      updateStatus.value = next;
    });
  }
  onBeforeUnmount(() => removeUpdateListener?.());

  return {
    isDesktop,
    loading,
    status,
    credentialsStatus,
    errorMessage,
    fetchStatus,
    saveCredentials,
    testConnection,
    createBackup,
    restoreBackup,
    openLogDirectory,
    reindex,
    updateStatus,
    checkForUpdates,
    downloadUpdate,
    cancelUpdate,
    installUpdate,
  };
}
