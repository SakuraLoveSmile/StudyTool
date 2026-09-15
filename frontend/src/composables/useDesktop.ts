import { ref, computed, onBeforeUnmount } from 'vue';
import axios from 'axios';

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
  [key: string]: unknown;
}

export interface DesktopLogQuery {
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
  entries: DesktopLogEntry[];
  total: number;
  page: number;
  pageSize: number;
  hasMore: boolean;
  scannedBytes: number;
  scanTruncated: boolean;
  unavailable: string[];
}

export interface DesktopDiagnosticExportResult {
  ok: boolean;
  canceled?: boolean;
  path?: string;
  entries?: number;
  truncated?: boolean;
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
      openLogDirectory(): void;
      /** Diagnostics (test build). Read-only: there is no delete/clear channel. */
      getLogMeta?(): Promise<DesktopLogMeta>;
      queryLogs?(params: DesktopLogQuery): Promise<DesktopLogQueryResult>;
      reportLog?(entries: unknown[]): Promise<{ ok: boolean; accepted: number; dropped: number }>;
      exportDiagnostics?(params: { from: number; to: number }): Promise<DesktopDiagnosticExportResult>;
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
            api_url: '',
            configured: data.baidu_ocr_configured,
          },
        };
      }
    } catch (err: any) {
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
        // In web fallback, check validate endpoint
        const valRes = await axios.post('/api/desktop/config/validate', {
          new_embedding_base_url: creds.embedding_base_url,
          new_embedding_model: creds.embedding_model,
        });
        if (valRes.data.allowed) {
          return { ok: true, message: '配置已校验' };
        }
        throw new Error(valRes.data.message || '配置未通过校验');
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
        const res = await axios.post('/api/desktop/test-connection', {
          service_type: serviceType,
          ...customConfig,
        });
        return res.data;
      } catch (err: any) {
        // Surface the backend's own reason (e.g. HTTPException `detail`) instead of the
        // generic axios "Request failed with status code 400".
        const detail = err?.response?.data?.detail;
        return {
          ok: false,
          service: serviceType,
          error_type: 'backend_error',
          message: typeof detail === 'string' ? detail : err.message,
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

  const openLogDirectory = () => {
    if (isDesktop.value && window.desktopAPI) {
      window.desktopAPI.openLogDirectory();
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
