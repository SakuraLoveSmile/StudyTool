import test from 'node:test';
import assert from 'node:assert';
import fs from 'fs';
import path from 'path';
import os from 'os';
import { BackendManager } from '../backend-manager';
import { CredentialsStore, SafeStorageInterface } from '../credentials-store';

function createTempDir(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'rag-cfg-rollback-test-'));
}

test('BackendManager: switchConfigWithRollback aborts if reading old credentials throws decryption error', async () => {
  const tmpDir = createTempDir();
  let decryptShouldFail = true;

  const mockStorage: SafeStorageInterface = {
    isEncryptionAvailable: () => true,
    encryptString: (s) => Buffer.from(`ENC:${s}`),
    decryptString: (b) => {
      if (decryptShouldFail) {
        throw new Error('DPAPI 密文损坏或由不同用户加密');
      }
      return b.toString().replace(/^ENC:/, '');
    },
  };

  const store = new CredentialsStore(tmpDir, mockStorage);
  // Write a dummy encrypted file directly
  fs.writeFileSync(path.join(tmpDir, 'credentials.enc'), Buffer.from('ENC:dummy'));

  const manager = new BackendManager(18764, '127.0.0.1', 'test-token', store, false);

  const res = await manager.switchConfigWithRollback({
    llm_api_key: 'new-key',
  });

  assert.strictEqual(res.ok, false);
  assert.match(res.message, /无法读取原凭据配置.*保留原文件/);
  // File must remain untouched
  assert.strictEqual(fs.existsSync(path.join(tmpDir, 'credentials.enc')), true);

  fs.rmSync(tmpDir, { recursive: true, force: true });
});

test('BackendManager: switchConfigWithRollback restores old credentials if new start fails', async () => {
  const tmpDir = createTempDir();

  const mockStorage: SafeStorageInterface = {
    isEncryptionAvailable: () => true,
    encryptString: (s) => Buffer.from(`ENC:${s}`),
    decryptString: (b) => b.toString().replace(/^ENC:/, ''),
  };

  const store = new CredentialsStore(tmpDir, mockStorage);
  store.update({ llm_api_key: 'initial-old-key', llm_model: 'gpt-3.5' });

  const manager = new BackendManager(18764, '127.0.0.1', 'test-token', store, false);

  // Mock drainAndStop and start
  (manager as any).drainAndStop = async () => true;

  let startCallCount = 0;
  (manager as any).start = async () => {
    startCallCount++;
    if (startCallCount === 1) {
      // First start is for the new credentials -> fails
      return false;
    }
    // Second start is for the restored old credentials -> succeeds
    return true;
  };
  (manager as any).forceKill = async () => {};
  (manager as any).waitForExit = async () => true;

  const res = await manager.switchConfigWithRollback({
    llm_api_key: 'new-failing-key',
    llm_model: 'gpt-4o',
  });

  assert.strictEqual(res.ok, false);
  assert.match(res.message, /已自动回滚至先前配置/);

  // Assert credentials store has been restored to old credentials
  const loaded = store.load();
  assert.strictEqual(loaded.llm_api_key, 'initial-old-key');
  assert.strictEqual(loaded.llm_model, 'gpt-3.5');

  fs.rmSync(tmpDir, { recursive: true, force: true });
});
