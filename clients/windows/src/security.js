const SERVICE_PARTITION = 'persist:connected-service';

function normalizeOrigin(input) {
  if (typeof input !== 'string' || input.length > 2048) throw new Error('请输入有效的 HTTPS 服务地址。');
  const value = input.trim();
  // URL normalizes dot paths and backslashes; reject them before parsing as well.
  if (!/^https:\/\/[^\s/?#\\]+\/?$/i.test(value)) throw new Error('服务地址必须是 HTTPS 域名，可带端口，不要填写路径、参数或片段。');
  let parsed;
  try { parsed = new URL(value); } catch { throw new Error('请输入有效的 HTTPS 服务地址。'); }
  if (parsed.protocol !== 'https:' || !parsed.hostname || parsed.username || parsed.password || parsed.pathname !== '/' || parsed.search || parsed.hash) {
    throw new Error('服务地址必须是 HTTPS 域名，不能包含账号、路径、参数或片段。');
  }
  return parsed.origin;
}

function navigationAction(url, origin) {
  try {
    const target = new URL(url);
    if (target.username || target.password) return 'deny';
    if (target.protocol === 'https:' && target.origin === origin) return 'allow';
    return ['https:', 'http:'].includes(target.protocol) ? 'external' : 'deny';
  } catch { return 'deny'; }
}

function isSettingsSender(event, settingsWindow, settingsURL) {
  if (!settingsWindow || settingsWindow.isDestroyed() || event.sender !== settingsWindow.webContents) return false;
  const frame = event.senderFrame;
  return !!frame && frame === event.sender.mainFrame && frame.url === settingsURL;
}

function guardedSettingsHandler(getWindow, settingsURL, handler) {
  return async (event, ...args) => {
    if (!isSettingsSender(event, getWindow(), settingsURL)) throw new Error('此操作只允许在本地连接设置中使用。');
    return handler(...args);
  };
}

function remoteWindowOptions(session) {
  return {
    width: 1200, height: 840, minWidth: 720, minHeight: 540,
    backgroundColor: '#f5f4f0', title: 'StudyTool Connected', show: false,
    webPreferences: {
      session, nodeIntegration: false, contextIsolation: true, sandbox: true,
      webSecurity: true, allowRunningInsecureContent: false, webviewTag: false,
      navigateOnDragDrop: false,
    },
  };
}

function protectRemoteContents(contents, origin, openExternal) {
  const navigate = (event, url) => {
    const action = navigationAction(url, origin);
    if (action === 'allow') return;
    event.preventDefault();
    if (action === 'external') void openExternal(url).catch(() => {});
  };
  contents.on('will-navigate', navigate);
  contents.on('will-redirect', navigate);
  contents.on('will-attach-webview', event => event.preventDefault());
  contents.setWindowOpenHandler(({ url }) => {
    const action = navigationAction(url, origin);
    if (action === 'allow') void contents.loadURL(url).catch(() => {});
    if (action === 'external') void openExternal(url).catch(() => {});
    return { action: 'deny' };
  });
}

async function clearServiceSession(serviceSession) {
  await serviceSession.closeAllConnections();
  await serviceSession.clearStorageData();
  await serviceSession.clearCache();
  await serviceSession.clearAuthCache();
}

module.exports = { SERVICE_PARTITION, normalizeOrigin, navigationAction, isSettingsSender, guardedSettingsHandler, remoteWindowOptions, protectRemoteContents, clearServiceSession };
