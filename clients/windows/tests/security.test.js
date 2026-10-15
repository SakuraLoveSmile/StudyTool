const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { normalizeOrigin, navigationAction, guardedSettingsHandler, remoteWindowOptions, protectRemoteContents, clearServiceSession } = require('../src/security');

test('HTTPS origin normalization accepts port, IDN and trailing slash', () => {
  assert.equal(normalizeOrigin(' HTTPS://Learn.Example.com:443/ '), 'https://learn.example.com');
  assert.equal(normalizeOrigin('https://learn.example.com:8443'), 'https://learn.example.com:8443');
  assert.equal(normalizeOrigin('https://学习.example.com'), 'https://xn--vjq503a.example.com');
});

test('service origins reject credential, insecure, path and URL parser normalization bypasses', () => {
  for (const url of [null, {}, '', 'http://localhost:8000', 'file:///test', 'javascript:alert(1)',
    'https://user:secret@learn.example.com', 'https://learn.example.com/library', 'https://learn.example.com/..',
    'https://learn.example.com/.', 'https://learn.example.com/?', 'https://learn.example.com/#',
    'https://learn.example.com?a=1', 'https://learn.example.com#abc', 'https://learn.example.com\\evil',
    'https://learn.example.com:99999', 'https://']) {
    assert.throws(() => normalizeOrigin(url), undefined, String(url));
  }
});

test('navigation remains in configured origin; http/https links are external only', () => {
  const origin = 'https://learn.example.com';
  assert.equal(navigationAction(`${origin}/login?next=%2Flibrary`, origin), 'allow');
  for (const url of ['https://external.example.com', 'http://learn.example.com', 'https://learn.example.com:444']) {
    assert.equal(navigationAction(url, origin), 'external');
  }
  for (const url of ['https://user:secret@external.example.com', 'file:///etc/passwd', 'javascript:alert(1)', 'data:text/html,a', 'studytool:test', 'invalid']) {
    assert.equal(navigationAction(url, origin), 'deny');
  }
});

test('actual IPC guard accepts only live settings main frame with exact local URL', async () => {
  const settingsURL = 'file:///application/src/settings.html';
  const frame = { url: settingsURL };
  const contents = { mainFrame: frame };
  const window = { isDestroyed: () => false, webContents: contents };
  let calls = 0;
  const handler = guardedSettingsHandler(() => window, settingsURL, input => { calls++; return input; });
  assert.equal(await handler({ sender: contents, senderFrame: frame }, 'https://example.com'), 'https://example.com');
  for (const event of [
    { sender: {}, senderFrame: frame },
    { sender: contents, senderFrame: { url: settingsURL } },
    { sender: contents, senderFrame: null },
  ]) await assert.rejects(() => handler(event), /本地连接设置/);
  frame.url = 'https://learn.example.com';
  await assert.rejects(() => handler({ sender: contents, senderFrame: frame }), /本地连接设置/);
  frame.url = settingsURL;
  window.isDestroyed = () => true;
  await assert.rejects(() => handler({ sender: contents, senderFrame: frame }), /本地连接设置/);
  assert.equal(calls, 1);
});

test('remote BrowserWindow never receives a preload or Node bridge', () => {
  const serviceSession = {};
  const { webPreferences } = remoteWindowOptions(serviceSession);
  assert.equal(webPreferences.session, serviceSession);
  assert.equal(webPreferences.nodeIntegration, false);
  assert.equal(webPreferences.contextIsolation, true);
  assert.equal(webPreferences.sandbox, true);
  assert.equal(webPreferences.webSecurity, true);
  assert.equal(webPreferences.webviewTag, false);
  assert.equal(Object.hasOwn(webPreferences, 'preload'), false);
});

test('attached navigation and popup handlers block escape and dangerous protocols', () => {
  const contents = new EventEmitter();
  contents.setWindowOpenHandler = handler => { contents.popup = handler; };
  const opened = [];
  const loaded = [];
  contents.loadURL = async url => { loaded.push(url); };
  protectRemoteContents(contents, 'https://learn.example.com', async url => { opened.push(url); });
  let blocked = 0;
  const event = { preventDefault: () => blocked++ };
  contents.emit('will-navigate', event, 'https://learn.example.com/library');
  assert.equal(blocked, 0);
  contents.emit('will-navigate', event, 'https://external.example.com');
  contents.emit('will-redirect', event, 'https://other.example.com');
  contents.emit('will-redirect', event, 'file:///etc/passwd');
  assert.equal(blocked, 3);
  assert.deepEqual(contents.popup({ url: 'javascript:alert(1)' }), { action: 'deny' });
  assert.deepEqual(contents.popup({ url: 'https://example.com' }), { action: 'deny' });
  assert.deepEqual(contents.popup({ url: 'https://learn.example.com/documents/1' }), { action: 'deny' });
  contents.emit('will-attach-webview', event);
  assert.equal(blocked, 4);
  assert.deepEqual(opened, ['https://external.example.com', 'https://other.example.com', 'https://example.com']);
  assert.deepEqual(loaded, ['https://learn.example.com/documents/1']);
});

test('service switch clears all storage, cache and HTTP authentication after closing connections', async () => {
  const calls = [];
  const fake = Object.fromEntries(['closeAllConnections', 'clearStorageData', 'clearCache', 'clearAuthCache'].map(name => [name, async () => { calls.push(name); }]));
  await clearServiceSession(fake);
  assert.deepEqual(calls, ['closeAllConnections', 'clearStorageData', 'clearCache', 'clearAuthCache']);
  fake.clearStorageData = async () => { throw new Error('disk error'); };
  await assert.rejects(() => clearServiceSession(fake), /disk error/);
});
