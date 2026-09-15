import test from 'node:test';
import assert from 'node:assert';
import fs from 'fs';
import path from 'path';
import os from 'os';
import { CredentialsStore, SafeStorageInterface } from '../credentials-store';

function createTempDir(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'rag-cred-test-'));
}

test('CredentialsStore: safeStorage unavailable rejects load and save, never writes plaintext', () => {
  const tmpDir = createTempDir();
  const mockStorage: SafeStorageInterface = {
    isEncryptionAvailable: () => false,
    encryptString: () => {
      throw new Error('Should not be called');
    },
    decryptString: () => {
      throw new Error('Should not be called');
    },
  };

  const store = new CredentialsStore(tmpDir, mockStorage);

  // Saving must throw and not create file
  assert.throws(
    () => {
      store.saveRaw({ llm_api_key: 'sk-test' });
    },
    /safeStorage \/ DPAPI.*不可用/
  );

  const encFile = path.join(tmpDir, 'credentials.enc');
  assert.strictEqual(fs.existsSync(encFile), false, 'Plaintext file must never be written');

  // If file exists, load must refuse to read when encryption is unavailable
  fs.writeFileSync(encFile, Buffer.from('dummy data'));
  assert.throws(
    () => {
      store.load();
    },
    /safeStorage \/ DPAPI.*不可用/
  );

  fs.rmSync(tmpDir, { recursive: true, force: true });
});

test('CredentialsStore: decryption failure preserves existing file and raises error', () => {
  const tmpDir = createTempDir();
  const encFile = path.join(tmpDir, 'credentials.enc');
  fs.writeFileSync(encFile, Buffer.from('corrupted_or_foreign_dpapi_payload'));

  const mockStorage: SafeStorageInterface = {
    isEncryptionAvailable: () => true,
    encryptString: (s) => Buffer.from(`ENC:${s}`),
    decryptString: () => {
      throw new Error('Windows DPAPI decryption failed');
    },
  };

  const store = new CredentialsStore(tmpDir, mockStorage);

  assert.throws(
    () => {
      store.load();
    },
    /凭据解密失败.*已保留原加密文件/
  );

  // Assert file still exists intact
  assert.strictEqual(fs.existsSync(encFile), true);
  assert.strictEqual(fs.readFileSync(encFile).toString(), 'corrupted_or_foreign_dpapi_payload');

  fs.rmSync(tmpDir, { recursive: true, force: true });
});

test('CredentialsStore: supports keep, replace, and clear operations atomically', () => {
  const tmpDir = createTempDir();
  const mockStorage: SafeStorageInterface = {
    isEncryptionAvailable: () => true,
    encryptString: (s) => Buffer.from(`ENC:${s}`),
    decryptString: (b) => b.toString().replace(/^ENC:/, ''),
  };

  const store = new CredentialsStore(tmpDir, mockStorage);

  // 1. Initial save with LLM and Embedding keys
  store.update({
    llm_base_url: 'https://api.openai.com/v1',
    llm_model: 'gpt-4o-mini',
    llm_api_key: 'sk-llm-initial',
    embedding_api_key: 'sk-emb-initial',
  });

  let loaded = store.load();
  assert.strictEqual(loaded.llm_api_key, 'sk-llm-initial');
  assert.strictEqual(loaded.embedding_api_key, 'sk-emb-initial');

  // 2. Update without providing llm_api_key or with empty string: MUST KEEP OLD KEY
  store.update({
    llm_model: 'gpt-4o',
    llm_api_key: '', // Empty string in UI form represents "keep"
  });

  loaded = store.load();
  assert.strictEqual(loaded.llm_model, 'gpt-4o');
  assert.strictEqual(loaded.llm_api_key, 'sk-llm-initial', 'Empty string must not overwrite existing key');

  // 3. Update with explicit replace
  store.update({
    llm_api_key: { action: 'replace', value: 'sk-llm-replaced' },
  });
  loaded = store.load();
  assert.strictEqual(loaded.llm_api_key, 'sk-llm-replaced');

  // 4. Update with explicit clear
  store.update({
    embedding_api_key: { action: 'clear' },
  });
  loaded = store.load();
  assert.strictEqual(loaded.embedding_api_key, undefined, 'Clear action must remove key');

  // 5. Check getStatus does NOT leak plain secrets
  const status = store.getStatus();
  assert.strictEqual(status.llm.configured, true);
  assert.strictEqual(status.embedding.configured, false);
  assert.strictEqual((status.llm as any).api_key, undefined);

  fs.rmSync(tmpDir, { recursive: true, force: true });
});

test('CredentialsStore: clear removes encrypted credentials file and returns empty', () => {
  const tmpDir = createTempDir();
  const mockStorage: SafeStorageInterface = {
    isEncryptionAvailable: () => true,
    encryptString: (s) => Buffer.from(`ENC:${s}`),
    decryptString: (b) => b.toString().replace(/^ENC:/, ''),
  };

  const store = new CredentialsStore(tmpDir, mockStorage);
  store.update({ llm_api_key: 'sk-llm-to-clear' });
  assert.strictEqual(store.load().llm_api_key, 'sk-llm-to-clear');

  store.clear();
  assert.deepStrictEqual(store.load(), {});
  assert.strictEqual(fs.existsSync(path.join(tmpDir, 'credentials.enc')), false);

  fs.rmSync(tmpDir, { recursive: true, force: true });
});
