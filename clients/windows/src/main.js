const { app, BrowserWindow, ipcMain, Menu, session, shell } = require('electron');
const { createStartupTrace } = require('./startup-trace');
const startupTrace = createStartupTrace(app);
startupTrace('main-entered', { electron: process.versions.electron, platform: process.platform, packaged: app.isPackaged });
const fs = require('node:fs/promises');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { readOrigin, saveOrigin } = require('./config');
const { claimSingleInstance } = require('./lifecycle');
const { resolveProfile } = require('./profile');
const { SERVICE_PARTITION, normalizeOrigin, guardedSettingsHandler, remoteWindowOptions, protectRemoteContents, clearServiceSession } = require('./security');

app.setName('StudyTool Connected');
const profilePath = resolveProfile(app);
startupTrace('profile-resolved', { profilePath });
app.setPath('userData', profilePath);
startupTrace('user-data-set');
app.setAppUserModelId('com.studytool.connected.windows');
const settingsURL = pathToFileURL(path.join(__dirname, 'settings.html')).href;
let settingsWindow = null;
let remoteWindow = null;
let origin = '';
let displayError = '';
let saving = false;
let serviceSession;
let configPath;

function denyPermissions(target) {
  target.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  target.setPermissionCheckHandler(() => false);
}

function showSettings(error = '') {
  displayError = error;
  const previousSettings = settingsWindow;
  const win = new BrowserWindow({
    width: 780, height: 700, minWidth: 560, minHeight: 600,
    title: '连接学习服务 · StudyTool Connected', backgroundColor: '#f5f4f0',
    webPreferences: {
      session: session.fromPartition('connected-settings'),
      preload: path.join(__dirname, 'preload.js'),
      nodeIntegration: false, contextIsolation: true, sandbox: true,
      webSecurity: true, webviewTag: false, navigateOnDragDrop: false,
    },
  });
  settingsWindow = win;
  startupTrace('settings-window-created', { windowId: win.id });
  win.webContents.on('did-finish-load', () => startupTrace('settings-loaded'));
  win.webContents.on('did-fail-load', (_event, code, description) => startupTrace('settings-load-failed', { code, description }));
  win.webContents.on('preload-error', (_event, _path, error) => startupTrace('settings-preload-failed', { message: error.message, stack: error.stack }));
  win.webContents.on('render-process-gone', (_event, details) => startupTrace('settings-renderer-gone', details));
  denyPermissions(win.webContents.session);
  win.webContents.on('will-navigate', event => event.preventDefault());
  win.webContents.on('will-redirect', event => event.preventDefault());
  win.webContents.on('will-attach-webview', event => event.preventDefault());
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  win.on('closed', () => { if (settingsWindow === win) settingsWindow = null; });
  if (previousSettings && !previousSettings.isDestroyed()) previousSettings.destroy();
  if (remoteWindow && !remoteWindow.isDestroyed()) remoteWindow.destroy();
  remoteWindow = null;
  void win.loadURL(settingsURL);
}

async function openService() {
  if (!origin) { showSettings(); return; }
  if (remoteWindow && !remoteWindow.isDestroyed()) { remoteWindow.show(); return; }
  const win = new BrowserWindow(remoteWindowOptions(serviceSession));
  remoteWindow = win;
  protectRemoteContents(win.webContents, origin, url => shell.openExternal(url));
  win.on('closed', () => { if (remoteWindow === win) remoteWindow = null; });
  win.webContents.on('did-fail-load', (_event, code, _description, _url, isMainFrame) => {
    if (isMainFrame && code !== -3 && remoteWindow === win) {
      showSettings('暂时无法连接学习服务。请检查地址、服务器和网络，然后重试。');
    }
  });
  try {
    await win.loadURL(`${origin}/library`);
    if (remoteWindow !== win || win.isDestroyed()) return;
    win.show();
    if (settingsWindow && !settingsWindow.isDestroyed()) settingsWindow.close();
  } catch {
    if (remoteWindow === win && !win.isDestroyed()) showSettings('暂时无法连接学习服务。请检查地址、服务器和网络，然后重试。');
  }
}

function registerSettingsHandler(channel, handler) {
  ipcMain.handle(channel, guardedSettingsHandler(() => settingsWindow, settingsURL, handler));
}

// Lock this userData profile before creating windows, reading config or opening a session.
const ownsInstance = claimSingleInstance(app, () => [settingsWindow, remoteWindow]);
startupTrace('single-instance-lock', { acquired: ownsInstance });
if (ownsInstance) app.whenReady().then(async () => {
  startupTrace('app-ready');
  await fs.mkdir(app.getPath('userData'), { recursive: true });
  startupTrace('user-data-created');
  configPath = path.join(app.getPath('userData'), 'service.json');
  serviceSession = session.fromPartition(SERVICE_PARTITION);
  denyPermissions(serviceSession);
  serviceSession.on('will-download', (event, item) => {
    try {
      if (new URL(item.getURL()).origin !== origin) event.preventDefault();
    } catch { event.preventDefault(); }
  });
  const saved = await readOrigin(configPath);
  startupTrace('config-read', { configured: !!saved.origin });
  origin = saved.origin;
  displayError = saved.error;

  registerSettingsHandler('connected:state', () => ({ origin, error: displayError }));
  registerSettingsHandler('connected:save', async input => {
    if (saving) return { ok: false, error: '正在保存，请稍候。' };
    saving = true;
    try {
      const next = normalizeOrigin(input);
      if (next !== origin) await clearServiceSession(serviceSession);
      await saveOrigin(configPath, next);
      origin = next;
      displayError = '';
      // Let the IPC reply reach the local renderer before it closes on success.
      setImmediate(() => void openService());
      return { ok: true };
    } catch (error) {
      return { ok: false, error: error.code ? '服务地址保存失败，请检查用户目录的写入权限。' : error.message };
    } finally { saving = false; }
  });
  registerSettingsHandler('connected:retry', () => {
    if (!origin || saving) return { ok: false, error: '请先保存服务地址。' };
    setImmediate(() => void openService());
    return { ok: true };
  });

  Menu.setApplicationMenu(Menu.buildFromTemplate([
    { label: '学习服务', submenu: [
      { label: '返回知识库', click: () => {
        if (remoteWindow && origin) void remoteWindow.loadURL(`${origin}/library`).catch(() => {});
        else void openService();
      } },
      { label: '后退', accelerator: 'Alt+Left', click: () => {
        const history = remoteWindow?.webContents.navigationHistory;
        if (history?.canGoBack()) history.goBack();
      } },
      { type: 'separator' },
      { label: '连接设置 / 切换服务', click: () => showSettings() },
      { label: '重试连接', click: () => { if (remoteWindow) remoteWindow.reload(); else void openService(); } },
      { type: 'separator' }, { role: 'quit', label: '退出' },
    ] },
    { label: '编辑', submenu: [{ role: 'undo' }, { role: 'redo' }, { type: 'separator' }, { role: 'cut' }, { role: 'copy' }, { role: 'paste' }, { role: 'selectAll' }] },
    { label: '视图', submenu: [{ role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' }, { role: 'togglefullscreen' }] },
  ]));
  if (origin) await openService(); else showSettings(displayError);
}).catch(error => {
  startupTrace('startup-rejected', { message: error.message, stack: error.stack });
  throw error;
});

app.on('window-all-closed', () => app.quit());
