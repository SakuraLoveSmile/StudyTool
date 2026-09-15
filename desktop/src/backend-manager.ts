import { ChildProcess, spawn } from 'child_process';
import path from 'path';
import fs from 'fs';
import os from 'os';
import http from 'http';
import { CredentialsStore, CredentialsData, CredentialsUpdateRequest } from './credentials-store';

export type ManagerLogLevel = 'INFO' | 'WARN' | 'ERROR';

/**
 * Optional diagnostics hook. Kept optional so existing construction sites (and tests)
 * keep working unchanged.
 */
export interface BackendManagerHooks {
  onEvent?: (
    event: string,
    level: ManagerLogLevel,
    message: string,
    fields?: Record<string, unknown>
  ) => void;
}

export class BackendManager {
  private childProcess: ChildProcess | null = null;
  private port: number;
  private host: string;
  private instanceToken: string;
  private credentialsStore: CredentialsStore;
  private isPackaged: boolean;
  private startupOutput = '';
  private hooks: BackendManagerHooks;

  constructor(
    port = 18764,
    host = '127.0.0.1',
    instanceToken: string,
    credentialsStore: CredentialsStore,
    isPackaged = false,
    hooks: BackendManagerHooks = {}
  ) {
    this.port = port;
    this.host = host;
    this.instanceToken = instanceToken;
    this.credentialsStore = credentialsStore;
    this.isPackaged = isPackaged;
    this.hooks = hooks;
  }

  private emit(
    event: string,
    level: ManagerLogLevel,
    message: string,
    fields: Record<string, unknown> = {}
  ): void {
    try {
      this.hooks.onEvent?.(event, level, message, fields);
    } catch {
      // Diagnostics must never break process management.
    }
  }

  private getBackendExecutable(): { command: string; args: string[]; cwd: string } {
    const isPackaged = this.isPackaged;
    if (isPackaged) {
      const exeName = process.platform === 'win32' ? 'backend.exe' : 'backend';
      const bundledExe = path.join((process as any).resourcesPath || process.cwd(), 'backend', exeName);
      return {
        command: bundledExe,
        args: [],
        cwd: path.dirname(bundledExe),
      };
    }

    // Development path
    const projectRoot = path.resolve(__dirname, '../../');
    const pythonScript = path.join(projectRoot, 'backend', 'app', 'main.py');
    const pythonCmd = process.platform === 'win32' ? 'python' : 'python3';

    return {
      command: pythonCmd,
      args: [pythonScript],
      cwd: path.join(projectRoot, 'backend'),
    };
  }

  /**
   * Start the backend process.
   * Passes the instance token and current credentials strictly through stdin.
   */
  public async start(): Promise<boolean> {
    if (this.childProcess) {
      if (this.childProcess.exitCode !== null) {
        this.childProcess = null;
      } else {
        console.warn('[BackendManager] Backend process already tracked; verifying readiness');
        return this.waitForReady(15000);
      }
    }

    const { command, args, cwd } = this.getBackendExecutable();
    this.startupOutput = '';
    let creds: CredentialsData = {};
    try {
      creds = this.credentialsStore.load();
    } catch (err: any) {
      console.warn('[BackendManager] Credentials could not be loaded; starting with empty credentials:', err.message);
    }

    const bootstrapPayload = {
      instance_token: this.instanceToken,
      port: this.port,
      host: this.host,
      llm_base_url: creds.llm_base_url || 'https://api.openai.com/v1',
      llm_model: creds.llm_model || 'gpt-4o-mini',
      llm_api_key: creds.llm_api_key || '',
      embedding_base_url: creds.embedding_base_url || 'https://api.openai.com/v1',
      embedding_model: creds.embedding_model || 'text-embedding-3-small',
      embedding_api_key: creds.embedding_api_key || '',
      baidu_ocr_api_url: creds.baidu_ocr_api_url || '',
      baidu_ocr_token: creds.baidu_ocr_token || '',
    };

    const spawnArgs = [...args, '--desktop', `--port=${this.port}`, `--host=${this.host}`];

    // Isolate environment: do NOT inherit development .env or model keys
    const env: NodeJS.ProcessEnv = {
      ...process.env,
      RAG_DESKTOP_MODE: '1',
      // Keep packaged diagnostics and data out of the read-only application
      // resources directory, including before the backend consumes stdin.
      RAG_DESKTOP_DATA_DIR: path.dirname(this.getDataDir()),
      LOG_DIR: this.getLogDir(),
      PORT: String(this.port),
      HOST: this.host,
      // One session id per launch, shared by desktop / backend / frontend log streams.
      RAG_SESSION_ID: process.env.RAG_SESSION_ID || '',
      RAG_APP_VERSION: process.env.RAG_APP_VERSION || '',
    };

    console.log(`[BackendManager] Spawning backend: ${command} ${spawnArgs.join(' ')}`);
    const startedAt = Date.now();
    this.emit('startup.backend.spawn', 'INFO', '正在启动本地后台服务', {
      service: 'backend',
      target: command,
    });

    return new Promise((resolve) => {
      let startupFailed = false;

      this.childProcess = spawn(command, spawnArgs, {
        cwd,
        env,
        stdio: ['pipe', 'pipe', 'pipe'],
        detached: false,
      });

      this.childProcess.on('error', (err) => {
        console.error('[BackendManager] Failed to spawn backend process:', err);
        this.recordStartupOutput('spawn error', err.message);
        this.emit('startup.backend.spawn_failed', 'ERROR', '后台进程无法启动', {
          error_type: err.name,
          error_message: err.message,
          duration_ms: Date.now() - startedAt,
        });
        startupFailed = true;
        this.childProcess = null;
        resolve(false);
      });

      this.childProcess.on('exit', (code, signal) => {
        console.log(`[BackendManager] Process exited with code ${code}, signal ${signal}`);
        if (code !== 0) {
          this.recordStartupOutput('process exit', `code=${String(code)} signal=${String(signal)}`);
        }
        this.emit(
          'backend.process.exit',
          code === 0 || code === null ? 'INFO' : 'ERROR',
          '后台进程已退出',
          { exit_code: code === null ? undefined : code, signal: signal || undefined }
        );
        this.childProcess = null;
      });

      this.childProcess.stdout?.on('data', (data) => {
        const text = data.toString();
        if (!text.includes('GET /api/desktop/status')) {
          console.log(`[Backend stdout]: ${text.trim()}`);
        }
      });

      this.childProcess.stderr?.on('data', (data) => {
        const text = data.toString();
        this.recordStartupOutput('stderr', text);
        console.error(`[Backend stderr]: ${text.trim()}`);
      });

      // Write bootstrap payload to stdin
      try {
        this.childProcess.stdin?.write(JSON.stringify(bootstrapPayload) + '\n');
      } catch (writeErr) {
        console.error('[BackendManager] Failed writing bootstrap payload to stdin:', writeErr);
        resolve(false);
        return;
      }

      // Probe authenticated status endpoint
      this.waitForReady(15000).then(async (ready) => {
        if (startupFailed || !ready) {
          console.error('[BackendManager] Backend process failed to become ready within timeout; ensuring termination');
          this.emit('startup.backend.ready_timeout', 'ERROR', '后台服务未在超时时间内就绪', {
            duration_ms: Date.now() - startedAt,
            timeout_config: '15000ms',
            detail: this.startupOutput.trim().slice(-2000),
          });
          if (this.childProcess) {
            await this.forceKill();
          }
          resolve(false);
        } else {
          this.emit('startup.backend.ready', 'INFO', '后台服务已就绪', {
            duration_ms: Date.now() - startedAt,
          });
          resolve(true);
        }
      });
    });
  }

  private recordStartupOutput(source: string, output: string): void {
    const sanitized = String(output)
      .replace(/(api[_-]?key|token|authorization|secret|password)[^=:\n]*[=:]\s*[^\s,;]+/gi, '$1=[REDACTED]')
      .trim();
    if (!sanitized) return;
    this.startupOutput = `${this.startupOutput}\n[${source}] ${sanitized}`.slice(-6000);
  }

  public getStartupFailureDetail(): string {
    return this.startupOutput.trim() || '后台进程未返回错误信息。';
  }

  /**
   * Probe backend readiness by checking /api/desktop/status with instance token.
   */
  public waitForReady(timeoutMs: number): Promise<boolean> {
    const startTime = Date.now();
    return new Promise((resolve) => {
      const check = () => {
        if (!this.childProcess) {
          resolve(false);
          return;
        }

        const req = http.request(
          `http://${this.host}:${this.port}/api/desktop/status`,
          {
            method: 'GET',
            headers: {
              'X-RAG-Instance-Token': this.instanceToken,
              Host: `${this.host}:${this.port}`,
            },
            timeout: 1000,
          },
          (res) => {
            if (res.statusCode === 200) {
              resolve(true);
            } else {
              retry();
            }
          }
        );

        req.on('error', () => {
          retry();
        });

        const retry = () => {
          if (Date.now() - startTime >= timeoutMs) {
            resolve(false);
          } else {
            setTimeout(check, 350);
          }
        };

        req.end();
      };

      check();
    });
  }

  /**
   * Graceful drain and stop.
   * Calls /api/desktop/drain, then /api/desktop/shutdown, and waits up to timeoutMs.
   */
  public async drainAndStop(
    leaseIdOrTimeout?: string | number,
    maybeTimeoutMs = 30000
  ): Promise<boolean> {
    if (!this.childProcess) {
      return true;
    }

    const leaseId = typeof leaseIdOrTimeout === 'string' ? leaseIdOrTimeout : undefined;
    const timeoutMs = typeof leaseIdOrTimeout === 'number' ? leaseIdOrTimeout : maybeTimeoutMs;

    try {
      // 1. Drain in-flight tasks and flush WAL
      const drainBody = leaseId ? { lease_id: leaseId } : {};
      await this.sendRequest('POST', '/api/desktop/drain', drainBody);
      // 2. Request shutdown
      const shutdownBody = leaseId ? { lease_id: leaseId } : {};
      await this.sendRequest('POST', '/api/desktop/shutdown', shutdownBody);
    } catch (err) {
      console.warn('[BackendManager] Warning during drain/shutdown request:', err);
    }

    // Wait for process exit
    const exited = await this.waitForExit(timeoutMs);
    return exited;
  }

  public waitForExit(timeoutMs: number): Promise<boolean> {
    if (!this.childProcess) return Promise.resolve(true);

    const proc = this.childProcess;
    if (proc.exitCode !== null) {
      this.childProcess = null;
      return Promise.resolve(true);
    }

    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        resolve(false);
      }, timeoutMs);

      proc.once('exit', () => {
        clearTimeout(timer);
        this.childProcess = null;
        resolve(true);
      });
    });
  }

  public async forceKill(): Promise<boolean> {
    if (!this.childProcess) return true;

    console.warn('[BackendManager] Force killing backend process');
    const proc = this.childProcess;
    try {
      if (process.platform === 'win32' && proc.pid) {
        const { execSync } = require('child_process');
        execSync(`taskkill /F /PID ${proc.pid} /T`);
      } else {
        proc.kill('SIGKILL');
      }
    } catch (err) {
      console.error('[BackendManager] Force kill error:', err);
      return false;
    }
    // Keep the process reference until the OS emits exit. A kill signal is not
    // proof that the child has actually stopped.
    return this.waitForExit(5000);
  }

  /**
   * Run an offline command (e.g. backup or restore) using the backend executable.
   */
  public async runOfflineTool(subcommand: 'backup' | 'restore', toolArgs: string[]): Promise<{ ok: boolean; output: string }> {
    if (this.childProcess) {
      throw new Error('无法在后台服务运行时执行离线工具，请先停止服务');
    }

    const { command, args, cwd } = this.getBackendExecutable();
    const fullArgs = [...args, subcommand, ...toolArgs];

    return new Promise((resolve, reject) => {
      console.log(`[BackendManager] Running offline tool: ${command} ${fullArgs.join(' ')}`);
      const proc = spawn(command, fullArgs, {
        cwd,
        env: {
          ...process.env,
          RAG_DESKTOP_MODE: '1',
          RAG_DESKTOP_DATA_DIR: path.dirname(this.getDataDir()),
          LOG_DIR: this.getLogDir(),
        },
        stdio: ['ignore', 'pipe', 'pipe'],
      });

      let stdout = '';
      let stderr = '';

      proc.stdout.on('data', (d) => (stdout += d.toString()));
      proc.stderr.on('data', (d) => (stderr += d.toString()));

      proc.on('close', (code) => {
        if (code === 0) {
          resolve({ ok: true, output: stdout });
        } else {
          reject(new Error(`离线工具执行失败 (退出代码 ${code}): ${stderr || stdout}`));
        }
      });

      proc.on('error', (err) => {
        reject(err);
      });
    });
  }

  /**
   * Safe config switch with automatic rollback on failure.
   * Shared by save, replace, and clear credentials:
   * 1. Confirm backend shutdown before touching credentials.
   * 2. Atomically save new encrypted config (or clear).
   * 3. Start backend and verify instance readiness.
   * 4. If new configuration fails to start: rollback to old credentials and restart old backend.
   */
  public async switchConfigWithRollback(
    updateReq: CredentialsUpdateRequest | null,
    leaseId?: string
  ): Promise<{ ok: boolean; message: string }> {
    let oldCreds: CredentialsData = {};
    let hadOldCreds = false;
    try {
      oldCreds = this.credentialsStore.load();
      hadOldCreds = Object.keys(oldCreds).length > 0;
    } catch (loadErr: any) {
      return {
        ok: false,
        message: `无法读取原凭据配置（解密或文件损坏: ${loadErr.message}），已中止修改并保留原文件`,
      };
    }

    // 1. Confirm shutdown before touching credentials (30s timeout guard)
    const stopped = await this.drainAndStop(leaseId, 30000);
    if (!stopped) {
      return {
        ok: false,
        message: '后台服务停机超时（30秒内未完全退出），为防止数据损坏已中止配置修改，原凭据未被变动',
      };
    }

    // 2. Atomically save new encrypted config or clear
    try {
      if (updateReq === null) {
        this.credentialsStore.clear();
      } else {
        this.credentialsStore.update(updateReq);
      }
    } catch (err: any) {
      console.error('[BackendManager] Failed updating credentials in store:', err);
      // Restart old instance before returning error
      await this.start();
      return { ok: false, message: `保存凭据失败: ${err.message}` };
    }

    // 3. Attempt start with new credentials
    const started = await this.start();
    if (started) {
      return { ok: true, message: updateReq === null ? '凭据已清除并生效' : '配置已更新并生效' };
    }

    // 4. Start failed: rollback to old credentials
    console.error('[BackendManager] New configuration failed to start; rolling back to old credentials');
    // Ensure failed process is dead and exited before touching filesystem
    await this.forceKill();
    await this.waitForExit(5000);

    try {
      if (hadOldCreds) {
        this.credentialsStore.saveRaw(oldCreds);
      } else {
        this.credentialsStore.clear();
      }
    } catch (saveErr: any) {
      console.error('[BackendManager] Failed rolling back credentials in store:', saveErr);
      return {
        ok: false,
        message: `新配置启动就绪失败，且尝试回滚旧凭据失败（${saveErr.message}）。请查看日志：${this.getLogDir()}`,
      };
    }

    // 5. Restart with old configuration
    const oldStarted = await this.start();
    const logDir = this.getLogDir();
    if (oldStarted) {
      return {
        ok: false,
        message: `新配置启动失败（无法通过就绪检查），已自动回滚至先前配置。详细日志请查看：${logDir}`,
      };
    } else {
      return {
        ok: false,
        message: `新配置启动失败且回滚原配置无法就绪，请查看日志文件进行定位排查：${logDir}`,
      };
    }
  }

  /**
   * Safe restore execution with strict CLI output verification, startup probe check,
   * and multi-stage rollback with failure scene preservation.
   */
  private restoreSnapshot(dataDir: string, snapshotDir: string): { ok: boolean; failedDir?: string; error?: string } {
    if (!fs.existsSync(snapshotDir)) {
      return { ok: false, error: '恢复前数据副本不存在' };
    }
    let failedDir: string | undefined;
    if (fs.existsSync(dataDir)) {
      failedDir = `${dataDir}-failed-restore-${Date.now()}-${Math.random().toString(16).slice(2, 10)}`;
      try {
        fs.renameSync(dataDir, failedDir);
      } catch (err) {
        return { ok: false, error: `失败现场无法保留：${err instanceof Error ? err.message : String(err)}` };
      }
    }
    try {
      fs.renameSync(snapshotDir, dataDir);
    } catch (err) {
      if (failedDir && !fs.existsSync(dataDir)) {
        try {
          fs.renameSync(failedDir, dataDir);
        } catch {
          // Preserve both the error and the original failed directory path.
        }
      }
      return { ok: false, failedDir, error: `恢复前数据切回失败：${err instanceof Error ? err.message : String(err)}` };
    }
    return { ok: true, failedDir };
  }

  public async executeRestoreWithRollback(
    archivePath: string,
    creds: any = {}
  ): Promise<{ ok: boolean; message: string; vector_locked?: boolean }> {
    const dataDir = this.getDataDir();
    const toolArgs = ['--archive', archivePath, '--target', dataDir];
    if (creds.embedding_model) {
      toolArgs.push('--expected-model', creds.embedding_model);
    }
    if (creds.embedding_base_url) {
      toolArgs.push('--expected-base-url', creds.embedding_base_url);
    }

    let runRes: { ok: boolean; output: string };
    try {
      runRes = await this.runOfflineTool('restore', toolArgs);
    } catch (toolErr: any) {
      // Offline tool itself failed (e.g. hash mismatch, tar corrupt, model mismatch)
      const restarted = await this.start();
      return {
        ok: false,
        message: restarted
          ? `恢复工具执行失败，原服务已恢复: ${toolErr.message}`
          : `恢复工具执行失败，原服务也未能就绪，请查看日志: ${toolErr.message}`,
      };
    }

    // Strict JSON parsing and verification of contract fields
    let restoreData: any = null;
    try {
      const lines = runRes.output.trim().split('\n');
      for (let i = lines.length - 1; i >= 0; i--) {
        const line = lines[i].trim();
        if (line.startsWith('{') && line.endsWith('}')) {
          const parsed = JSON.parse(line);
          if (parsed && typeof parsed === 'object') {
            restoreData = parsed;
            break;
          }
        }
      }
    } catch (parseErr: any) {
      console.error('[BackendManager] Failed to parse restore CLI output:', parseErr);
    }

    if (
      !restoreData ||
      restoreData.ok !== true ||
      !restoreData.restored_to ||
      path.resolve(String(restoreData.restored_to)) !== path.resolve(dataDir) ||
      typeof restoreData.pre_restore_snapshot !== 'string' ||
      !restoreData.pre_restore_snapshot
    ) {
      console.error('[BackendManager] Restore CLI output failed strict contract validation:', runRes.output);
      const snapshot = typeof restoreData?.pre_restore_snapshot === 'string' ? restoreData.pre_restore_snapshot : '';
      if (snapshot) {
        const rollback = this.restoreSnapshot(dataDir, snapshot);
        if (rollback.ok) {
          const restarted = await this.start();
          return {
            ok: false,
            message: restarted
              ? '恢复程序输出校验失败，但已根据恢复记录切回旧数据并验证服务就绪'
              : '恢复程序输出校验失败，旧数据已切回但服务未能就绪，请查看日志',
          };
        }
      }
      // Without a verified snapshot path, leave the live directory untouched.
      const restarted = await this.start();
      return {
        ok: false,
        message: restarted
          ? '恢复程序输出校验失败，已中止恢复；当前服务已启动但未确认数据版本，请检查日志'
          : '恢复程序输出校验失败，且服务未能就绪；当前数据目录未再修改，请查看日志',
      };
    }

    // Attempt start with restored data
    const started = await this.start();
    if (!started) {
      console.error('[BackendManager] Restored backend failed to start; rolling back to pre-restore snapshot');
      const killed = await this.forceKill();
      if (killed === false) {
        return {
          ok: false,
          message: '恢复数据后服务未退出，无法安全切回原数据；当前恢复现场已保留，请查看日志',
        };
      }

      const rollback = this.restoreSnapshot(dataDir, restoreData.pre_restore_snapshot);
      if (!rollback.ok) {
        return {
          ok: false,
          message: `恢复服务失败，${rollback.error || '无法切回恢复前数据'}；失败现场${rollback.failedDir ? `保存在 ${rollback.failedDir}` : '已保留'}，未完成回滚`,
        };
      }

      // Restart old backend
      const oldStarted = await this.start();
      return {
        ok: false,
        message: oldStarted
          ? '恢复数据后服务启动就绪验证失败，已保留失败现场并安全回滚至恢复前数据'
          : '恢复数据后服务启动就绪验证失败，原数据已回滚，但服务未能正常就绪，请查看日志',
      };
    }

    const successMsg = restoreData.vector_locked
      ? '数据已恢复，但检测到历史向量来源不明已锁定检索与追加，请在设置中重建索引'
      : '数据已安全恢复';
    return {
      ok: true,
      message: successMsg,
      vector_locked: Boolean(restoreData.vector_locked),
    };
  }

  public getDataDir(): string {
    if (process.env.RAG_DESKTOP_DATA_DIR) {
      return path.resolve(process.env.RAG_DESKTOP_DATA_DIR, 'data');
    }
    let rootDir = '';
    if (process.platform === 'win32' || process.env.LOCALAPPDATA) {
      rootDir = path.join(
        process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local'),
        'RAGStudyAssistant'
      );
    } else if (process.platform === 'darwin') {
      rootDir = path.join(os.homedir(), 'Library', 'Application Support', 'RAGStudyAssistant');
    } else {
      rootDir = path.join(
        process.env.XDG_DATA_HOME || path.join(os.homedir(), '.local', 'share'),
        'RAGStudyAssistant'
      );
    }
    return path.join(rootDir, 'data');
  }

  public getLogDir(): string {
    if (process.platform === 'win32' || process.env.LOCALAPPDATA) {
      return path.join(
        process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local'),
        'RAGStudyAssistant',
        'logs'
      );
    } else if (process.platform === 'darwin') {
      return path.join(os.homedir(), 'Library', 'Application Support', 'RAGStudyAssistant', 'logs');
    } else {
      return path.join(
        process.env.XDG_DATA_HOME || path.join(os.homedir(), '.local', 'share'),
        'RAGStudyAssistant',
        'logs'
      );
    }
  }

  public async getStatus(): Promise<{ idle: boolean; active_tasks: number }> {
    try {
      const res = await this.sendRequest('GET', '/api/desktop/status');
      return {
        idle: Boolean(res.idle),
        active_tasks: Number(res.active_tasks || 0),
      };
    } catch {
      return { idle: false, active_tasks: 1 };
    }
  }

  private sendRequest(method: string, apiPath: string, body?: any): Promise<any> {
    return new Promise((resolve, reject) => {
      const postData = body ? JSON.stringify(body) : '';
      const req = http.request(
        `http://${this.host}:${this.port}${apiPath}`,
        {
          method,
          headers: {
            'Content-Type': 'application/json',
            'X-RAG-Instance-Token': this.instanceToken,
            Host: `${this.host}:${this.port}`,
            Origin: `http://${this.host}:${this.port}`,
            ...(body ? { 'Content-Length': Buffer.byteLength(postData) } : {}),
          },
          timeout: 10000,
        },
        (res) => {
          let raw = '';
          res.on('data', (chunk) => (raw += chunk));
          res.on('end', () => {
            try {
              const parsed = raw ? JSON.parse(raw) : null;
              if (res.statusCode && res.statusCode >= 400) {
                reject(new Error(parsed?.detail || `HTTP ${res.statusCode}: ${raw}`));
              } else {
                resolve(parsed);
              }
            } catch {
              if (res.statusCode && res.statusCode >= 400) {
                reject(new Error(`HTTP ${res.statusCode}: ${raw}`));
              } else {
                resolve(raw);
              }
            }
          });
        }
      );

      req.on('error', (err) => reject(err));
      if (postData) req.write(postData);
      req.end();
    });
  }
}
