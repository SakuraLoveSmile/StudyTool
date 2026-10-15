const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { resolveProfile } = require('../src/profile');
const root = path.resolve('test-temp');
const app = { isPackaged: true, getPath: () => path.resolve('app-data') };

test('installed client ignores development profile; developer client may isolate its data', () => {
  const env = { STUDYTOOL_CONNECTED_DEV_PROFILE: path.resolve('developer-profile') };
  assert.equal(resolveProfile(app, [], env), path.resolve('app-data', 'StudyTool Connected'));
  assert.equal(resolveProfile({ ...app, isPackaged: false }, [], env), env.STUDYTOOL_CONNECTED_DEV_PROFILE);
});

test('smoke profile accepts only a new direct child with a bounded name inside system temp', () => {
  const allowed = path.join(root, 'studytool-connected-smoke-1234');
  assert.equal(resolveProfile(app, [`--smoke-test-profile=${allowed}`], {}, root, () => false), allowed);
  for (const value of [path.join(root, 'other'), path.resolve('outside', 'studytool-connected-smoke-1'),
    path.join(root, 'nested', 'studytool-connected-smoke-1'), 'studytool-connected-smoke-relative', '']) {
    assert.throws(() => resolveProfile(app, [`--smoke-test-profile=${value}`], {}, root, () => false), /Smoke profile/);
  }
  assert.throws(() => resolveProfile(app, [`--smoke-test-profile=${allowed}`], {}, root, () => true), /Smoke profile/);
  assert.throws(() => resolveProfile(app, [`--smoke-test-profile=${allowed}`, `--smoke-test-profile=${allowed}`], {}, root, () => false), /Only one/);
});
