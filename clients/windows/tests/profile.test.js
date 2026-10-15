const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const os = require('node:os');
const { resolveProfile } = require('../src/profile');
const app = { isPackaged: true, getPath: () => path.resolve('app-data') };

test('installed client ignores development profile; developer client may isolate its data', () => {
  const env = { STUDYTOOL_CONNECTED_DEV_PROFILE: path.resolve('developer-profile') };
  assert.equal(resolveProfile(app, [], env), path.resolve('app-data', 'StudyTool Connected'));
  assert.equal(resolveProfile({ ...app, isPackaged: false }, [], env), env.STUDYTOOL_CONNECTED_DEV_PROFILE);
});

test('smoke profile accepts only a new direct child with a bounded name inside system temp', t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'studytool-profile-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const allowed = path.join(root, 'studytool-connected-smoke-1234');
  assert.equal(resolveProfile(app, [`--smoke-test-profile=${allowed}`], {}, root), path.join(fs.realpathSync.native(root), path.basename(allowed)));
  for (const value of [path.join(root, 'other'), path.resolve('outside', 'studytool-connected-smoke-1'),
    path.join(root, 'nested', 'studytool-connected-smoke-1'), 'studytool-connected-smoke-relative', '']) {
    assert.throws(() => resolveProfile(app, [`--smoke-test-profile=${value}`], {}, root, () => false), /Smoke profile/);
  }
  assert.throws(() => resolveProfile(app, [`--smoke-test-profile=${allowed}`], {}, root, () => true), /Smoke profile/);
  assert.throws(() => resolveProfile(app, [`--smoke-test-profile=${allowed}`, `--smoke-test-profile=${allowed}`], {}, root, () => false), /Only one/);
});

test('actual parent aliases resolve to the same temp directory without relaxing the fresh-child boundary', t => {
  const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'studytool-profile-alias-'));
  t.after(() => fs.rmSync(fixture, { recursive: true, force: true }));
  const actual = path.join(fixture, 'actual');
  const alias = path.join(fixture, 'alias');
  const outside = path.join(fixture, 'outside');
  fs.mkdirSync(actual);
  fs.mkdirSync(outside);
  // Junctions do not require symlink privileges on Windows; POSIX resolves this as a directory symlink.
  fs.symlinkSync(actual, alias, 'junction');
  const name = 'studytool-connected-smoke-alias';
  const canonical = path.join(fs.realpathSync.native(actual), name);
  assert.equal(resolveProfile(app, [`--smoke-test-profile=${path.join(actual, name)}`], {}, alias), canonical);
  assert.equal(resolveProfile(app, [`--smoke-test-profile=${path.join(alias, name)}`], {}, actual), canonical);
  assert.equal(fs.existsSync(canonical), false);
  fs.mkdirSync(canonical);
  assert.throws(() => resolveProfile(app, [`--smoke-test-profile=${path.join(alias, name)}`], {}, actual), /already exists/);
  assert.throws(() => resolveProfile(app, [`--smoke-test-profile=${path.join(outside, name)}`], {}, actual), /canonicalParent.*canonicalNodeTemp/);
  const traversal = `${actual}${path.sep}nested${path.sep}..${path.sep}studytool-connected-smoke-traversal`;
  assert.throws(() => resolveProfile(app, [`--smoke-test-profile=${traversal}`], {}, actual), /invalid/);
  fs.symlinkSync(outside, path.join(actual, 'escape'), 'junction');
  assert.throws(() => resolveProfile(app, [`--smoke-test-profile=${path.join(actual, 'escape', name)}`], {}, actual), /outside temp/);
  fs.symlinkSync(outside, path.join(actual, 'studytool-connected-smoke-existing-link'), 'junction');
  assert.throws(() => resolveProfile(app, [`--smoke-test-profile=${path.join(alias, 'studytool-connected-smoke-existing-link')}`], {}, actual), /already exists/);
  const vanishedTarget = path.join(fixture, 'vanished');
  fs.mkdirSync(vanishedTarget);
  fs.symlinkSync(vanishedTarget, path.join(actual, 'studytool-connected-smoke-dangling-link'), 'junction');
  fs.rmdirSync(vanishedTarget);
  assert.throws(() => resolveProfile(app, [`--smoke-test-profile=${path.join(alias, 'studytool-connected-smoke-dangling-link')}`], {}, actual), /already exists/);
});
