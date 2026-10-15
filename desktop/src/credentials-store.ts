import fs from 'fs';
import path from 'path';
import { configDir } from './app-paths';

export interface SafeStorageInterface {
  isEncryptionAvailable(): boolean;
  encryptString(plainText: string): Buffer;
  decryptString(encrypted: Buffer): string;
}

function getDefaultSafeStorage(): SafeStorageInterface {
  try {
    const electron = require('electron');
    return electron.safeStorage || {
      isEncryptionAvailable: () => false,
      encryptString: () => { throw new Error('safeStorage unavailable'); },
      decryptString: () => { throw new Error('safeStorage unavailable'); },
    };
  } catch {
    return {
      isEncryptionAvailable: () => false,
      encryptString: () => { throw new Error('safeStorage unavailable'); },
      decryptString: () => { throw new Error('safeStorage unavailable'); },
    };
  }
}

export interface CredentialsData {
  llm_base_url?: string;
  llm_model?: string;
  llm_api_key?: string;
  embedding_base_url?: string;
  embedding_model?: string;
  embedding_api_key?: string;
  baidu_ocr_api_url?: string;
  baidu_ocr_token?: string;
}

export type KeyOperation = 'keep' | 'replace' | 'clear';

export interface KeyActionValue {
  action: KeyOperation;
  value?: string;
}

export interface CredentialsUpdateRequest {
  llm_base_url?: string;
  llm_model?: string;
  llm_api_key?: string | KeyActionValue;
  embedding_base_url?: string;
  embedding_model?: string;
  embedding_api_key?: string | KeyActionValue;
  baidu_ocr_api_url?: string;
  baidu_ocr_token?: string | KeyActionValue;
}

export interface CredentialsStatus {
  llm: {
    base_url: string;
    model: string;
    configured: boolean;
  };
  embedding: {
    base_url: string;
    model: string;
    configured: boolean;
    locked?: boolean;
  };
  baidu_ocr: {
    api_url: string;
    configured: boolean;
  };
}

export class CredentialsStore {
  private configDir: string;
  private filePath: string;
  private storage: SafeStorageInterface;

  constructor(customConfigDir?: string, storageImpl?: SafeStorageInterface) {
    this.storage = storageImpl || getDefaultSafeStorage();
    this.configDir = customConfigDir || configDir();

    fs.mkdirSync(this.configDir, { recursive: true });
    this.filePath = path.join(this.configDir, 'credentials.enc');
  }

  /**
   * Load and decrypt stored credentials.
   * STRICT: NEVER falls back to plain text. If safeStorage is unavailable or decryption fails,
   * it preserves the existing file untouched and throws an informative error.
   */
  public load(): CredentialsData {
    if (!fs.existsSync(this.filePath)) {
      return {};
    }

    if (!this.storage.isEncryptionAvailable()) {
      throw new Error('系统安全存储 (safeStorage / DPAPI) 不可用，拒绝读取凭据');
    }

    const raw = fs.readFileSync(this.filePath);
    if (!raw || raw.length === 0) {
      return {};
    }

    try {
      const decryptedStr = this.storage.decryptString(raw);
      return JSON.parse(decryptedStr);
    } catch (err: any) {
      console.error('[CredentialsStore] Decryption failed (file preserved):', err.message);
      throw new Error('凭据解密失败（可能是设备更换或凭据损坏），已保留原加密文件，请在设置中重新录入');
    }
  }

  /**
   * Encrypt and atomically save credentials using safeStorage (Windows DPAPI).
   * Writes to a temporary file first and renames atomically to prevent corruption.
   */
  public saveRaw(data: CredentialsData): void {
    if (!this.storage.isEncryptionAvailable()) {
      throw new Error('系统安全存储 (safeStorage / DPAPI) 不可用，禁止保存凭据');
    }

    const jsonStr = JSON.stringify(data);
    const encryptedBuffer = this.storage.encryptString(jsonStr);

    const tempPath = `${this.filePath}.tmp.${Date.now()}.${Math.random().toString(36).slice(2, 8)}`;
    try {
      fs.writeFileSync(tempPath, encryptedBuffer, { mode: 0o600 });
      fs.renameSync(tempPath, this.filePath);
    } catch (err) {
      if (fs.existsSync(tempPath)) {
        try {
          fs.unlinkSync(tempPath);
        } catch {
          // ignore cleanup error
        }
      }
      throw new Error(`保存凭据文件失败: ${err}`);
    }
  }

  /**
   * Update credentials with explicit "keep", "replace", or "clear" actions.
   * If input key is omitted or empty string, existing secret is preserved ("keep").
   */
  public update(req: CredentialsUpdateRequest): CredentialsData {
    let existing: CredentialsData = {};
    try {
      existing = this.load();
    } catch (err) {
      console.warn('[CredentialsStore] Warning on loading existing credentials:', err);
      existing = {};
    }

    const next: CredentialsData = { ...existing };

    if (req.llm_base_url !== undefined) {
      next.llm_base_url = req.llm_base_url.trim();
    }
    if (req.llm_model !== undefined) {
      next.llm_model = req.llm_model.trim();
    }
    this.applyKeyAction(next, 'llm_api_key', req.llm_api_key, existing.llm_api_key);

    if (req.embedding_base_url !== undefined) {
      next.embedding_base_url = req.embedding_base_url.trim();
    }
    if (req.embedding_model !== undefined) {
      next.embedding_model = req.embedding_model.trim();
    }
    this.applyKeyAction(next, 'embedding_api_key', req.embedding_api_key, existing.embedding_api_key);

    if (req.baidu_ocr_api_url !== undefined) {
      next.baidu_ocr_api_url = req.baidu_ocr_api_url.trim();
    }
    this.applyKeyAction(next, 'baidu_ocr_token', req.baidu_ocr_token, existing.baidu_ocr_token);

    this.saveRaw(next);
    return next;
  }

  private applyKeyAction(
    target: CredentialsData,
    field: keyof CredentialsData,
    actionOrVal: string | KeyActionValue | undefined,
    oldVal?: string
  ): void {
    if (actionOrVal === undefined) {
      // Not specified: keep old value
      target[field] = oldVal;
      return;
    }

    if (typeof actionOrVal === 'string') {
      const trimmed = actionOrVal.trim();
      if (trimmed === '') {
        // Empty string means keep existing
        target[field] = oldVal;
      } else {
        // Non-empty string means replace
        target[field] = trimmed;
      }
      return;
    }

    // KeyActionValue object
    if (actionOrVal.action === 'clear') {
      delete target[field];
    } else if (actionOrVal.action === 'replace') {
      target[field] = (actionOrVal.value || '').trim();
    } else {
      // 'keep'
      target[field] = oldVal;
    }
  }

  /**
   * Clears all credentials and deletes the encrypted file.
   */
  public clear(): void {
    if (fs.existsSync(this.filePath)) {
      fs.unlinkSync(this.filePath);
    }
  }

  /**
   * Return non-sensitive status for frontend. Never leaks plain secrets!
   */
  public getStatus(hasExistingVectors = false): CredentialsStatus {
    let data: CredentialsData = {};
    try {
      data = this.load();
    } catch {
      data = {};
    }

    return {
      llm: {
        base_url: data.llm_base_url || 'https://api.openai.com/v1',
        model: data.llm_model || 'gpt-4o-mini',
        configured: Boolean(data.llm_api_key && data.llm_api_key.trim()),
      },
      embedding: {
        base_url: data.embedding_base_url || 'https://api.openai.com/v1',
        model: data.embedding_model || 'text-embedding-3-small',
        configured: Boolean(data.embedding_api_key && data.embedding_api_key.trim()),
        locked: hasExistingVectors,
      },
      baidu_ocr: {
        api_url: data.baidu_ocr_api_url || '',
        configured: Boolean(data.baidu_ocr_api_url && data.baidu_ocr_token),
      },
    };
  }
}
