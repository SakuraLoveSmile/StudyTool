import test from 'node:test';
import assert from 'node:assert';
import { EventEmitter } from 'events';
import { BackendManager } from '../backend-manager';
import { CredentialsStore, SafeStorageInterface } from '../credentials-store';

class MockChildProcess extends EventEmitter {
  public pid = 99999;
  public exitCode: number | null = null;
  public killed = false;

  public kill(signal?: string) {
    this.killed = true;
    this.exitCode = 0;
    this.emit('exit', 0, signal);
    return true;
  }
}

test('BackendManager: waitForExit resolves true immediately if child process already exited', async () => {
  const store = new CredentialsStore('/tmp', {
    isEncryptionAvailable: () => true,
    encryptString: (s) => Buffer.from(s),
    decryptString: (b) => b.toString(),
  });
  const manager = new BackendManager(18764, '127.0.0.1', 'token-test', store);

  // If childProcess is null, returns true immediately
  const exited1 = await manager.waitForExit(1000);
  assert.strictEqual(exited1, true);

  // If childProcess has exitCode, returns true immediately
  const mockProc = new MockChildProcess();
  mockProc.exitCode = 0;
  (manager as any).childProcess = mockProc;
  const exited2 = await manager.waitForExit(1000);
  assert.strictEqual(exited2, true);
  assert.strictEqual((manager as any).childProcess, null);
});

test('BackendManager: waitForExit resolves true when exit event is emitted', async () => {
  const store = new CredentialsStore('/tmp', {
    isEncryptionAvailable: () => true,
    encryptString: (s) => Buffer.from(s),
    decryptString: (b) => b.toString(),
  });
  const manager = new BackendManager(18764, '127.0.0.1', 'token-test', store);
  const mockProc = new MockChildProcess();
  (manager as any).childProcess = mockProc;

  setTimeout(() => {
    mockProc.exitCode = 0;
    mockProc.emit('exit', 0, null);
  }, 50);

  const exited = await manager.waitForExit(1000);
  assert.strictEqual(exited, true);
  assert.strictEqual((manager as any).childProcess, null);
});

test('BackendManager: waitForExit times out without auto force-killing child process', async () => {
  const store = new CredentialsStore('/tmp', {
    isEncryptionAvailable: () => true,
    encryptString: (s) => Buffer.from(s),
    decryptString: (b) => b.toString(),
  });
  const manager = new BackendManager(18764, '127.0.0.1', 'token-test', store);
  const mockProc = new MockChildProcess();
  (manager as any).childProcess = mockProc;

  const exited = await manager.waitForExit(100);
  assert.strictEqual(exited, false, 'Should report false on timeout');
  assert.strictEqual(mockProc.killed, false, 'Must NOT auto force-kill on timeout');
  assert.notStrictEqual((manager as any).childProcess, null, 'Child process must remain tracked until explicit kill/exit');
});

test('BackendManager: forceKill reports failure and keeps tracking an unconfirmed child', async () => {
  const store = new CredentialsStore('/tmp', {
    isEncryptionAvailable: () => true,
    encryptString: (s) => Buffer.from(s),
    decryptString: (b) => b.toString(),
  });
  const manager = new BackendManager(18764, '127.0.0.1', 'token-test', store);
  const mockProc = new MockChildProcess();
  (manager as any).childProcess = mockProc;
  (manager as any).waitForExit = async () => false;

  const killed = await manager.forceKill();
  assert.strictEqual(killed, false);
  assert.strictEqual((manager as any).childProcess, mockProc);
});
