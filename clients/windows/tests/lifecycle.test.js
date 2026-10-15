const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { claimSingleInstance } = require('../src/lifecycle');

test('second process exits before startup and never attaches a focus handler', () => {
  const app = new EventEmitter();
  let quits = 0;
  app.requestSingleInstanceLock = () => false;
  app.quit = () => quits++;
  assert.equal(claimSingleInstance(app, () => { throw new Error('must not read windows'); }), false);
  assert.equal(quits, 1);
  assert.equal(app.listenerCount('second-instance'), 0);
});

test('second launch focuses settings first and restores a minimized remote window when settings is closed', () => {
  const app = new EventEmitter();
  app.requestSingleInstanceLock = () => true;
  app.quit = () => { throw new Error('must not quit owner'); };
  const calls = [];
  const settings = {
    isDestroyed: () => false, isMinimized: () => false,
    restore: () => calls.push('settings:restore'), show: () => calls.push('settings:show'), focus: () => calls.push('settings:focus'),
  };
  const remote = {
    isDestroyed: () => false, isMinimized: () => true,
    restore: () => calls.push('remote:restore'), show: () => calls.push('remote:show'), focus: () => calls.push('remote:focus'),
  };
  assert.equal(claimSingleInstance(app, () => [settings, remote]), true);
  app.emit('second-instance');
  assert.deepEqual(calls, ['settings:show', 'settings:focus']);
  settings.isDestroyed = () => true;
  app.emit('second-instance');
  assert.deepEqual(calls.slice(2), ['remote:restore', 'remote:show', 'remote:focus']);
  remote.isDestroyed = () => true;
  app.emit('second-instance');
  assert.equal(calls.length, 5);
});
