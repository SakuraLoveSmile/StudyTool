const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { EventEmitter } = require('node:events');
const { createStartupTrace } = require('../src/startup-trace');

test('smoke startup trace preserves a new profile and observes errors without replacing handlers', t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'studytool-trace-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const profile = path.join(root, 'studytool-connected-smoke-test');
  const fakeProcess = new EventEmitter();
  let stderr = '';
  fakeProcess.stderr = { write: message => { stderr += message; } };
  const trace = createStartupTrace({ isPackaged: true }, { argv: [`--smoke-test-profile=${profile}`], tempRoot: root, processLike: fakeProcess });
  assert.equal(fs.existsSync(profile), false, 'diagnostics must not create or invalidate the required fresh profile');
  trace('main-entered');
  fakeProcess.emit('uncaughtExceptionMonitor', new Error('startup failure'));
  assert.equal(fakeProcess.listenerCount('uncaughtException'), 0);
  assert.equal(fakeProcess.listenerCount('unhandledRejection'), 0);
  const lines = fs.readFileSync(trace.file, 'utf8').trim().split('\n').map(JSON.parse);
  assert.deepEqual(lines.map(line => line.event), ['smoke-paths', 'main-entered', 'uncaught-main-exception']);
  assert.equal(lines[0].canonicalNodeTemp, fs.realpathSync.native(root));
  assert.equal(lines[0].canonicalParent, fs.realpathSync.native(root));
  assert.match(stderr, /startup failure/);
  const original = fs.readFileSync(trace.file, 'utf8');
  const duplicate = createStartupTrace({}, { argv: [`--smoke-test-profile=${profile}`], tempRoot: root, processLike: fakeProcess });
  duplicate('must-not-overwrite');
  assert.equal(fs.readFileSync(trace.file, 'utf8'), original);
});

test('ordinary launches install no trace listeners and invalid smoke paths cannot write files', () => {
  const fakeProcess = new EventEmitter();
  fakeProcess.stderr = { write: () => {} };
  const ordinary = createStartupTrace({}, { argv: [], processLike: fakeProcess });
  ordinary('no-file');
  const invalid = createStartupTrace({}, { argv: ['--smoke-test-profile=/outside'], processLike: fakeProcess });
  invalid('no-file');
  assert.equal(fakeProcess.eventNames().length, 0);
});
