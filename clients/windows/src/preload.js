const { contextBridge, ipcRenderer } = require('electron');

// This preload is attached only to the packaged, local settings window.
contextBridge.exposeInMainWorld('connectedSettings', Object.freeze({
  getState: () => ipcRenderer.invoke('connected:state'),
  save: origin => ipcRenderer.invoke('connected:save', origin),
  retry: () => ipcRenderer.invoke('connected:retry'),
}));
