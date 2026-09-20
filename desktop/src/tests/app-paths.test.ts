/**
 * Tests for the application data paths and the one-time legacy root migration.
 *
 * 产品更名为 StudyTool / 学习助手 后，数据根目录由 RAGStudyAssistant 改为 StudyTool。
 * 迁移契约：旧目录存在且新目录不存在时整体改名；两者都存在时保留新目录、
 * 不动旧目录；env 覆盖（测试/沙盒隔离）完全不触发迁移；迁移失败时路径解析
 * 回退到旧目录，绝不把用户带进空目录。
 */

import test from 'node:test';
import assert from 'node:assert';
import fs from 'fs';
import os from 'os';
import path from 'path';
import {
  APP_DATA_ROOT_NAME,
  DATA_DIR_ENV,
  LEGACY_APP_DATA_ROOT_NAME,
  appDataRootForBase,
  configDir,
  dataDir,
  ensureAppDataMigration,
  logDir,
  migrateLegacyRoot,
  platformAppDataRoot,
  platformAppDataRootForBase,
} from '../app-paths';

function tempDir(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'app-paths-'));
}

function legacyRoot(base: string): string {
  return path.join(base, LEGACY_APP_DATA_ROOT_NAME);
}

function targetRoot(base: string): string {
  return path.join(base, APP_DATA_ROOT_NAME);
}

function seedLegacy(base: string): void {
  fs.mkdirSync(path.join(legacyRoot(base), 'data'), { recursive: true });
  fs.writeFileSync(path.join(legacyRoot(base), 'data', 'app.db'), 'payload');
  fs.mkdirSync(path.join(legacyRoot(base), 'logs'), { recursive: true });
  fs.mkdirSync(path.join(legacyRoot(base), 'config'), { recursive: true });
}

test('root name is StudyTool; legacy name is RAGStudyAssistant', () => {
  assert.strictEqual(APP_DATA_ROOT_NAME, 'StudyTool');
  assert.strictEqual(LEGACY_APP_DATA_ROOT_NAME, 'RAGStudyAssistant');
});

test('legacy root is renamed wholesale when the new root does not exist yet', () => {
  const base = tempDir();
  seedLegacy(base);

  migrateLegacyRoot(base);

  assert.ok(!fs.existsSync(legacyRoot(base)), '旧目录应已不存在');
  assert.strictEqual(
    fs.readFileSync(path.join(targetRoot(base), 'data', 'app.db'), 'utf-8'),
    'payload'
  );
  assert.ok(fs.existsSync(path.join(targetRoot(base), 'logs')));
  assert.ok(fs.existsSync(path.join(targetRoot(base), 'config')));
  // 迁移后解析到新根目录。
  assert.strictEqual(appDataRootForBase(base), targetRoot(base));
});

test('an existing new root wins: the legacy root is left untouched', () => {
  const base = tempDir();
  seedLegacy(base);
  fs.mkdirSync(path.join(targetRoot(base), 'data'), { recursive: true });
  fs.writeFileSync(path.join(targetRoot(base), 'data', 'app.db'), 'new');

  migrateLegacyRoot(base);

  assert.strictEqual(
    fs.readFileSync(path.join(targetRoot(base), 'data', 'app.db'), 'utf-8'),
    'new'
  );
  assert.ok(fs.existsSync(legacyRoot(base)), '旧目录必须保留，绝不被删除');
  assert.strictEqual(appDataRootForBase(base), targetRoot(base));
});

test('a fresh machine resolves to the new root and creates nothing', () => {
  const base = tempDir();
  assert.strictEqual(appDataRootForBase(base), targetRoot(base));
  assert.strictEqual(platformAppDataRootForBase(base), targetRoot(base));
  assert.deepStrictEqual(fs.readdirSync(base), []);
});

test('platform root falls back to the legacy root until the migration lands', () => {
  const base = tempDir();
  fs.mkdirSync(path.join(legacyRoot(base), 'data'), { recursive: true });

  // 迁移前：两种解析都回退旧目录，用户不会面对空库。
  assert.strictEqual(platformAppDataRootForBase(base), legacyRoot(base));
  assert.strictEqual(appDataRootForBase(base), legacyRoot(base));

  migrateLegacyRoot(base);

  assert.strictEqual(platformAppDataRootForBase(base), targetRoot(base));
  assert.strictEqual(appDataRootForBase(base), targetRoot(base));
});

test('env override resolves directly and never triggers migration', () => {
  const base = tempDir();
  const override = path.join(base, 'isolated-root');
  seedLegacy(base);

  const root = appDataRootForBase(base, { [DATA_DIR_ENV]: override });

  assert.strictEqual(root, path.resolve(override));
  assert.ok(fs.existsSync(legacyRoot(base)), 'env 覆盖路径不迁移旧目录');
  assert.ok(!fs.existsSync(targetRoot(base)));
});

test('a blank env override falls through to the platform default root', () => {
  const base = tempDir();
  assert.strictEqual(appDataRootForBase(base, { [DATA_DIR_ENV]: '   ' }), targetRoot(base));
});

test('migration failure keeps the legacy root as the resolved path', () => {
  const base = tempDir();
  seedLegacy(base);

  // 模拟 rename 失败（跨卷 EXDEV / 权限 EACCES）：迁移静默回退。
  const originalRename = fs.renameSync;
  (fs as { renameSync: typeof fs.renameSync }).renameSync = () => {
    throw new Error('EXDEV: cross-device link not permitted');
  };
  try {
    migrateLegacyRoot(base);
  } finally {
    (fs as { renameSync: typeof fs.renameSync }).renameSync = originalRename;
  }

  assert.ok(fs.existsSync(path.join(legacyRoot(base), 'data', 'app.db')), '旧数据仍在');
  assert.ok(!fs.existsSync(targetRoot(base)), '失败时不创建半成品新目录');
  // 关键：解析回退到旧目录，用户不会面对空库。
  assert.strictEqual(appDataRootForBase(base), legacyRoot(base));
  assert.strictEqual(platformAppDataRootForBase(base), legacyRoot(base));
});

test('ensureAppDataMigration is idempotent and skips env override', () => {
  const base = tempDir();
  seedLegacy(base);

  // 带 env 覆盖（测试/沙盒隔离）：跳过迁移。
  const previous = process.env[DATA_DIR_ENV];
  process.env[DATA_DIR_ENV] = path.join(base, 'isolated-root');
  try {
    ensureAppDataMigration();
    ensureAppDataMigration();
    assert.ok(fs.existsSync(legacyRoot(base)), 'env 覆盖时旧目录原样保留');
  } finally {
    if (previous === undefined) delete process.env[DATA_DIR_ENV];
    else process.env[DATA_DIR_ENV] = previous;
  }
});

test('data follows the env override; logs and config stay on the platform root', () => {
  // 与更名前语义逐条对齐：getDataDir() 读 STUDYTOOL_DESKTOP_DATA_DIR，
  // getLogDir()/配置目录不读（沙盒隔离契约依赖此行为）。
  const base = tempDir();
  const previous = process.env[DATA_DIR_ENV];
  process.env[DATA_DIR_ENV] = base;
  try {
    const overrideRoot = path.resolve(base);
    assert.strictEqual(dataDir(), path.join(overrideRoot, 'data'));

    const platformRoot = platformAppDataRoot();
    assert.strictEqual(logDir(), path.join(platformRoot, 'logs'));
    assert.strictEqual(configDir(), path.join(platformRoot, 'config'));
    assert.ok(!logDir().startsWith(overrideRoot), '日志目录不得被数据目录覆盖改道');
    assert.ok(!configDir().startsWith(overrideRoot), '配置目录不得被数据目录覆盖改道');
  } finally {
    if (previous === undefined) delete process.env[DATA_DIR_ENV];
    else process.env[DATA_DIR_ENV] = previous;
  }
});
