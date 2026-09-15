import { app, BrowserWindow, dialog, ipcMain, shell, session } from 'electron';
import path from 'path';
import fs from 'fs';
import crypto from 'crypto';
import os from 'os';
import http from 'http';
import { autoUpdater } from 'electron-updater';
import { CancellationToken } from 'electron-updater/out/types';
import { isPortAvailable } from './port-checker';
import { CredentialsStore, CredentialsUpdateRequest } from './credentials-store';
import { BackendManager } from './backend-manager';

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
  autoUpdater.on('error', (err) => publishUpdateState({ state: 'error', error: err.message || '更新服务不可用' }));
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
backendManager = new BackendManager(PORT, HOST, instanceToken, credentialsStore, app.isPackaged);

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
  });

  // Load the application
  await mainWindow.loadURL(BASE_URL);
}

// 3. Application Lifecycle
app.whenReady().then(async () => {
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
    const choice = dialog.showMessageBoxSync({
      type: 'error',
      title: '端口占用冲突',
      message: `端口 ${PORT} 已被其他程序占用。`,
      detail: `桌面版固定使用 http://${HOST}:${PORT} 保证安全来源与反馈组件正常工作。请关闭冲突程序后重试。`,
      buttons: ['重试', '退出程序'],
      defaultId: 0,
    });

    if (choice === 1) {
      isQuitting = true;
      app.quit();
      return;
    }
    portAvailable = await isPortAvailable(PORT, HOST);
  }

  // Start backend process
  let backendStarted = false;
  let backendStartupError = '';
  try {
    backendStarted = await backendManager!.start();
  } catch (err: any) {
    backendStartupError = err?.message || String(err);
  }
  if (!backendStarted) {
    dialog.showMessageBoxSync({
      type: 'error',
      title: '后台启动失败',
      message: '无法启动本地后台服务或未通过就绪验证。',
      detail: [
        backendStartupError || backendManager!.getStartupFailureDetail(),
        `日志目录：${backendManager!.getLogDir()}`,
        '请修复日志中指出的问题后重新启动。',
      ].join('\n'),
      buttons: ['确定'],
    });
    isQuitting = true;
    app.quit();
    return;
  }

  // Setup IPC Handlers
  setupIpcHandlers();
  setupAutoUpdater();

  // Create Window
  try {
    await createWindow();
  } catch (err: any) {
    dialog.showMessageBoxSync({
      type: 'error',
      title: '窗口启动失败',
      message: '本地后台已启动，但桌面窗口无法加载。',
      detail: [
        err?.message || String(err),
        `日志目录：${backendManager!.getLogDir()}`,
      ].join('\n'),
      buttons: ['确定'],
    });
    await backendManager!.forceKill();
    isQuitting = true;
    app.quit();
    return;
  }
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
    try {
      const res = await makeBackendRequest('POST', '/api/desktop/test-connection', {
        service_type: serviceType,
        base_url: customConfig?.base_url,
        model: customConfig?.model,
        api_key: customConfig?.api_key,
        baidu_ocr_url: customConfig?.baidu_ocr_url,
        baidu_ocr_token: customConfig?.baidu_ocr_token,
      });
      return res.data;
    } catch (err: any) {
      return { ok: false, message: `连接测试请求失败: ${err.message}` };
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
    let logDir = '';
    if (process.platform === 'win32' || process.env.LOCALAPPDATA) {
      logDir = path.join(
        process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local'),
        'RAGStudyAssistant',
        'logs'
      );
    } else if (process.platform === 'darwin') {
      logDir = path.join(os.homedir(), 'Library', 'Application Support', 'RAGStudyAssistant', 'logs');
    } else {
      logDir = path.join(
        process.env.XDG_DATA_HOME || path.join(os.homedir(), '.local', 'share'),
        'RAGStudyAssistant',
        'logs'
      );
    }
    fs.mkdirSync(logDir, { recursive: true });
    shell.openPath(logDir);
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
