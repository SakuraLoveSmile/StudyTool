/**
 * Tests for the appearance preference store (T3).
 *
 * The file is the single source of truth on desktop: it must live apart from the
 * credentials store, reject/ignore anything that is not a known preference, and keep
 * writes atomic so a crash never leaves a half-written theme file behind.
 */

import test from 'node:test';
import assert from 'node:assert';
import fs from 'fs';
import os from 'os';
import path from 'path';
import {
  ThemePreferenceStore,
  normalizeThemePreference,
  resolveEffectiveTheme,
  windowBackgroundFor,
} from '../theme-prefs';

function tempDir(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'rag-theme-'));
}

test('missing file defaults to system', () => {
  const store = new ThemePreferenceStore(tempDir());
  assert.strictEqual(store.load(), 'system');
});

test('save then load round-trips each preference', () => {
  const dir = tempDir();
  const store = new ThemePreferenceStore(dir);
  for (const pref of ['dark', 'light', 'system'] as const) {
    store.save(pref);
    assert.strictEqual(store.load(), pref);
    assert.strictEqual(
      JSON.parse(fs.readFileSync(path.join(dir, 'theme.json'), 'utf-8')).preference,
      pref
    );
  }
});

test('corrupt or hostile content falls back to system instead of throwing', () => {
  const dir = tempDir();
  fs.writeFileSync(path.join(dir, 'theme.json'), '{not json');
  assert.strictEqual(new ThemePreferenceStore(dir).load(), 'system');

  fs.writeFileSync(path.join(dir, 'theme.json'), JSON.stringify({ preference: 'sepia' }));
  assert.strictEqual(new ThemePreferenceStore(dir).load(), 'system');

  fs.writeFileSync(path.join(dir, 'theme.json'), JSON.stringify({ preference: { evil: 1 } }));
  assert.strictEqual(new ThemePreferenceStore(dir).load(), 'system');

  fs.writeFileSync(path.join(dir, 'theme.json'), '   ');
  assert.strictEqual(new ThemePreferenceStore(dir).load(), 'system');
});

test('save normalizes illegal input instead of persisting it', () => {
  const dir = tempDir();
  const store = new ThemePreferenceStore(dir);
  store.save('midnight' as never);
  assert.strictEqual(store.load(), 'system');
});

test('normalize accepts exactly the three states', () => {
  assert.strictEqual(normalizeThemePreference('system'), 'system');
  assert.strictEqual(normalizeThemePreference('light'), 'light');
  assert.strictEqual(normalizeThemePreference('dark'), 'dark');
  for (const bad of ['', 'DARK', 'auto', 0, null, undefined, [], {}]) {
    assert.strictEqual(normalizeThemePreference(bad), 'system', String(bad));
  }
});

test('system resolves through the injected detector; explicit prefs win', () => {
  assert.strictEqual(resolveEffectiveTheme('system', () => true), 'dark');
  assert.strictEqual(resolveEffectiveTheme('system', () => false), 'light');
  // 手动选择不被系统覆盖——检测器无论报什么都返回显式偏好。
  assert.strictEqual(resolveEffectiveTheme('dark', () => false), 'dark');
  assert.strictEqual(resolveEffectiveTheme('light', () => true), 'light');
});

test('window background matches the effective theme (no cold-start white flash)', () => {
  assert.strictEqual(windowBackgroundFor('dark'), '#1c1917');
  assert.strictEqual(windowBackgroundFor('light'), '#fafaf9');
  // system 由当前系统结论决定；node 环境下检测器缺省为 false（light）。
  assert.strictEqual(windowBackgroundFor('system'), '#fafaf9');
});

test('theme file lives in its own config dir, separate from credentials', () => {
  const dir = tempDir();
  new ThemePreferenceStore(dir).save('dark');
  const files = fs.readdirSync(dir);
  assert.deepStrictEqual(files, ['theme.json']);
});
