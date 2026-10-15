import { contextBridge, ipcRenderer } from 'electron';

/**
 * 首屏前的主题初始化（T3）：同步 IPC 拿主进程偏好并立刻写到 <html>。
 * 这是窗口冷启动不闪白的关键一环——异步 invoke 等到回值时第一帧已经画出去了。
 * 失败时保持默认浅色，网页侧仍可正常切换。
 */
const initialTheme = (() => {
  try {
    const t = ipcRenderer.sendSync('desktop:theme-sync') as
      | { preference?: string; effective?: string }
      | undefined;
    const effective = t && (t.effective === 'dark' || t.effective === 'light') ? t.effective : 'light';
    const root = document.documentElement;
    root.dataset.theme = effective;
    root.style.colorScheme = effective;
    return t;
  } catch {
    return undefined;
  }
})();

// Expose safe, whitelisted desktop APIs to the renderer window
contextBridge.exposeInMainWorld('desktopAPI', {
  isDesktop: true,

  // Credentials & Config
  getCredentialsStatus: () => ipcRenderer.invoke('desktop:getCredentialsStatus'),
  saveCredentials: (data: any) => ipcRenderer.invoke('desktop:saveCredentials', data),
  clearCredentials: () => ipcRenderer.invoke('desktop:clearCredentials'),
  testConnection: (serviceType: string, customConfig?: any) =>
    ipcRenderer.invoke('desktop:testConnection', serviceType, customConfig),

  // System status
  getSystemStatus: () => ipcRenderer.invoke('desktop:getSystemStatus'),

  // Backup & Restore Dialogs
  selectBackupFile: () => ipcRenderer.invoke('desktop:selectBackupFile'),
  selectSaveBackupLocation: () => ipcRenderer.invoke('desktop:selectSaveBackupLocation'),
  createBackup: (destinationDir?: string) => ipcRenderer.invoke('desktop:createBackup', destinationDir),
  restoreBackup: (archivePath: string) => ipcRenderer.invoke('desktop:restoreBackup', archivePath),

  // Platform actions
  reindex: () => ipcRenderer.invoke('desktop:reindex'),
  getUpdateStatus: () => ipcRenderer.invoke('desktop:getUpdateStatus'),
  checkForUpdates: () => ipcRenderer.invoke('desktop:checkForUpdates'),
  downloadUpdate: () => ipcRenderer.invoke('desktop:downloadUpdate'),
  cancelUpdate: () => ipcRenderer.invoke('desktop:cancelUpdate'),
  installUpdate: () => ipcRenderer.invoke('desktop:installUpdate'),
  onUpdateStatus: (callback: (status: any) => void) => {
    const listener = (_event: Electron.IpcRendererEvent, status: any) => callback(status);
    ipcRenderer.on('desktop:update-status', listener);
    return () => ipcRenderer.removeListener('desktop:update-status', listener);
  },
  openExternal: (url: string) => ipcRenderer.invoke('desktop:openExternal', url),
  openLogDirectory: () => ipcRenderer.invoke('desktop:openLogDirectory'),

  // Appearance: main process owns the preference file; renderer only consumes it.
  initialTheme,
  getThemePreference: () => ipcRenderer.invoke('desktop:getThemePreference'),
  setThemePreference: (preference: string) =>
    ipcRenderer.invoke('desktop:setThemePreference', preference),
  onThemeChanged: (callback: (payload: { preference: string; effective: string }) => void) => {
    const listener = (_event: Electron.IpcRendererEvent, payload: any) => callback(payload);
    ipcRenderer.on('desktop:theme-changed', listener);
    return () => ipcRenderer.removeListener('desktop:theme-changed', listener);
  },

  // Diagnostics (test build): read-only viewer, frontend error reporting and export.
  // There is deliberately no delete/clear channel — logs stay append-only for the user.
  getLogMeta: () => ipcRenderer.invoke('desktop:getLogMeta'),
  queryLogs: (params: any) => ipcRenderer.invoke('desktop:queryLogs', params),
  /** Stops a running query in the worker; the renderer still guards against stale replies. */
  cancelLogQuery: (requestId: string) => ipcRenderer.invoke('desktop:cancelLogQuery', requestId),
  reportLog: (entries: any) => ipcRenderer.invoke('desktop:reportLog', entries),
  exportDiagnostics: (params: any) => ipcRenderer.invoke('desktop:exportDiagnostics', params),
  /**
   * 反馈附件自动采集：无参数受限通道。时间窗/来源/会话/预算全部由主进程决定，
   * 渲染进程不能指定路径、范围或文件名。
   */
  getFeedbackLogs: () => ipcRenderer.invoke('desktop:getFeedbackLogs'),
});
