import { contextBridge, ipcRenderer } from 'electron';

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

  // Diagnostics (test build): read-only viewer, frontend error reporting and export.
  // There is deliberately no delete/clear channel — logs stay append-only for the user.
  getLogMeta: () => ipcRenderer.invoke('desktop:getLogMeta'),
  queryLogs: (params: any) => ipcRenderer.invoke('desktop:queryLogs', params),
  reportLog: (entries: any) => ipcRenderer.invoke('desktop:reportLog', entries),
  exportDiagnostics: (params: any) => ipcRenderer.invoke('desktop:exportDiagnostics', params),
});
