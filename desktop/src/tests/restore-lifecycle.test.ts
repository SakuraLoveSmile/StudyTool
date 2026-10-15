import test from 'node:test';
import assert from 'node:assert';
import fs from 'fs';
import path from 'path';
import os from 'os';
import { BackendManager } from '../backend-manager';
import { CredentialsStore, SafeStorageInterface } from '../credentials-store';

function createTempDir(prefix: string): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

const dummyStorage: SafeStorageInterface = {
  isEncryptionAvailable: () => true,
  encryptString: (s) => Buffer.from(s),
  decryptString: (b) => b.toString(),
};

test('BackendManager: executeRestoreWithRollback rejects invalid JSON CLI output and restores previous snapshot', async () => {
  const rootDir = createTempDir('rag-test-restore-invalid-');
  const dataDir = path.join(rootDir, 'data');
  fs.mkdirSync(dataDir, { recursive: true });
  fs.writeFileSync(path.join(dataDir, 'original_file.txt'), 'original content');

  const snapshotDir = path.join(rootDir, 'data-pre-restore-12345-abcd');
  fs.mkdirSync(snapshotDir, { recursive: true });
  fs.writeFileSync(path.join(snapshotDir, 'original_file.txt'), 'original content');

  process.env.STUDYTOOL_DESKTOP_DATA_DIR = rootDir;

  const store = new CredentialsStore(rootDir, dummyStorage);
  const manager = new BackendManager(18764, '127.0.0.1', 'token-test', store, false);

  // Mock runOfflineTool to output corrupt non-JSON lines
  (manager as any).runOfflineTool = async () => ({
    ok: true,
    output: 'Traceback (most recent call last):\nValueError: Something broke',
  });

  let restarted = false;
  (manager as any).start = async () => {
    restarted = true;
    return true;
  };

  const res = await manager.executeRestoreWithRollback('/path/to/archive.tar.gz');

  assert.strictEqual(res.ok, false);
  assert.match(res.message, /恢复程序输出校验失败/);
  assert.strictEqual(restarted, true);

  fs.rmSync(rootDir, { recursive: true, force: true });
});

test('BackendManager: executeRestoreWithRollback rejects CLI output missing required contract fields (ok, restored_to, pre_restore_snapshot)', async () => {
  const rootDir = createTempDir('rag-test-restore-missing-fields-');
  const dataDir = path.join(rootDir, 'data');
  fs.mkdirSync(dataDir, { recursive: true });
  process.env.STUDYTOOL_DESKTOP_DATA_DIR = rootDir;

  const store = new CredentialsStore(rootDir, dummyStorage);
  const manager = new BackendManager(18764, '127.0.0.1', 'token-test', store, false);

  // Missing pre_restore_snapshot
  (manager as any).runOfflineTool = async () => ({
    ok: true,
    output: JSON.stringify({ ok: true, restored_to: dataDir }),
  });

  (manager as any).start = async () => true;

  const res = await manager.executeRestoreWithRollback('/path/to/archive.tar.gz');

  assert.strictEqual(res.ok, false);
  assert.match(res.message, /恢复程序输出校验失败/);

  fs.rmSync(rootDir, { recursive: true, force: true });
});

test('BackendManager: executeRestoreWithRollback preserves failure scene and rolls back on startup probe failure', async () => {
  const rootDir = createTempDir('rag-test-restore-start-fail-');
  const dataDir = path.join(rootDir, 'data');
  fs.mkdirSync(dataDir, { recursive: true });
  fs.writeFileSync(path.join(dataDir, 'restored_corrupted_file.txt'), 'corrupted');

  const snapshotDir = path.join(rootDir, 'data-pre-restore-99999-ef01');
  fs.mkdirSync(snapshotDir, { recursive: true });
  fs.writeFileSync(path.join(snapshotDir, 'healthy_old_file.txt'), 'healthy data');

  process.env.STUDYTOOL_DESKTOP_DATA_DIR = rootDir;

  const store = new CredentialsStore(rootDir, dummyStorage);
  const manager = new BackendManager(18764, '127.0.0.1', 'token-test', store, false);

  (manager as any).runOfflineTool = async () => ({
    ok: true,
    output: JSON.stringify({
      ok: true,
      restored_to: dataDir,
      pre_restore_snapshot: snapshotDir,
      has_vectors: true,
      vector_locked: false,
    }),
  });

  let startCalls = 0;
  (manager as any).start = async () => {
    startCalls++;
    if (startCalls === 1) {
      // First start with restored data fails
      return false;
    }
    // Second start after rollback succeeds
    return true;
  };
  (manager as any).forceKill = async () => {};
  (manager as any).waitForExit = async () => true;

  const res = await manager.executeRestoreWithRollback('/path/to/archive.tar.gz');

  assert.strictEqual(res.ok, false);
  assert.match(res.message, /已保留失败现场并安全回滚/);

  // Assert failure scene directory was preserved
  const entries = fs.readdirSync(rootDir);
  const failedRestoreDir = entries.find((e) => e.startsWith('data-failed-restore-'));
  assert.ok(failedRestoreDir, 'Expected data-failed-restore-* directory to exist');
  assert.strictEqual(
    fs.existsSync(path.join(rootDir, failedRestoreDir, 'restored_corrupted_file.txt')),
    true
  );

  // Assert dataDir was restored from snapshot
  assert.strictEqual(
    fs.existsSync(path.join(dataDir, 'healthy_old_file.txt')),
    true
  );

  fs.rmSync(rootDir, { recursive: true, force: true });
});

test('BackendManager: executeRestoreWithRollback returns ok and vector_locked flag when startup probe succeeds', async () => {
  const rootDir = createTempDir('rag-test-restore-success-');
  const dataDir = path.join(rootDir, 'data');
  fs.mkdirSync(dataDir, { recursive: true });

  const snapshotDir = path.join(rootDir, 'data-pre-restore-11111-2222');
  fs.mkdirSync(snapshotDir, { recursive: true });

  process.env.STUDYTOOL_DESKTOP_DATA_DIR = rootDir;

  const store = new CredentialsStore(rootDir, dummyStorage);
  const manager = new BackendManager(18764, '127.0.0.1', 'token-test', store, false);

  (manager as any).runOfflineTool = async () => ({
    ok: true,
    output: JSON.stringify({
      ok: true,
      restored_to: dataDir,
      pre_restore_snapshot: snapshotDir,
      has_vectors: true,
      vector_locked: true,
      vector_lock_reason: '已有向量但来源信息缺失',
    }),
  });

  (manager as any).start = async () => true;

  const res = await manager.executeRestoreWithRollback('/path/to/archive.tar.gz');

  assert.strictEqual(res.ok, true);
  assert.strictEqual(res.vector_locked, true);
  assert.match(res.message, /已锁定检索与追加/);

  fs.rmSync(rootDir, { recursive: true, force: true });
});
