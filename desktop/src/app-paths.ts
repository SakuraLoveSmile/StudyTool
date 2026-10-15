import fs from 'fs';
import path from 'path';
import os from 'os';

/**
 * 应用数据路径真源。产品更名为 StudyTool / 学习助手 后，数据根目录由
 * RAGStudyAssistant 改为 StudyTool。
 *
 * 路径解析是纯净的：任何调用都不会改动磁盘。旧根目录的一次性迁移由
 * ensureAppDataMigration() 在主进程启动时显式执行一次。
 *
 * 纯 Node 模块：不 import electron，主进程各存储组件与 BackendManager 共用，
 * node --test 也可直接单测。
 */

/** 当前数据根目录名。 */
export const APP_DATA_ROOT_NAME = 'StudyTool';
/** 更名前使用的数据根目录名，仅用于一次性迁移识别。 */
export const LEGACY_APP_DATA_ROOT_NAME = 'RAGStudyAssistant';
/** 桌面主进程注入的数据根目录覆盖（测试/沙盒隔离用）：指向根目录，其下再有 data/logs/config。 */
export const DATA_DIR_ENV = 'STUDYTOOL_DESKTOP_DATA_DIR';

function platformBaseDir(): string {
  if (process.platform === 'win32' || process.env.LOCALAPPDATA) {
    return process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local');
  }
  if (process.platform === 'darwin') {
    return path.join(os.homedir(), 'Library', 'Application Support');
  }
  return process.env.XDG_DATA_HOME || path.join(os.homedir(), '.local', 'share');
}

/**
 * 一次性迁移：旧根目录存在且新根目录不存在时，整体改名为新根目录。
 * - 两者都已存在：保留新目录，不动旧目录（不覆盖、不删除，绝不丢数据）；
 * - 改名失败（跨卷、权限等）：静默回退，后续仍解析到旧目录，功能不中断。
 */
export function migrateLegacyRoot(baseDir: string): void {
  const legacy = path.join(baseDir, LEGACY_APP_DATA_ROOT_NAME);
  const target = path.join(baseDir, APP_DATA_ROOT_NAME);
  try {
    if (!fs.existsSync(legacy) || fs.existsSync(target)) return;
    fs.renameSync(legacy, target);
  } catch {
    // 迁移失败不阻塞启动：继续使用旧目录。
  }
}

/**
 * 平台默认数据根目录解析（纯净，无磁盘副作用，不读 env 覆盖）。
 * 若旧根目录仍在而新根目录不存在（迁移未执行或执行失败），解析到旧根目录——
 * 宁可沿用旧路径，也绝不让用户面对空目录。
 */
export function platformAppDataRootForBase(baseDir: string): string {
  const target = path.join(baseDir, APP_DATA_ROOT_NAME);
  const legacy = path.join(baseDir, LEGACY_APP_DATA_ROOT_NAME);
  if (!fs.existsSync(target) && fs.existsSync(legacy)) return legacy;
  return target;
}

/**
 * 数据根目录解析（仅供 data 目录使用）。env 覆盖（测试/沙盒隔离）时直接 resolve，
 * 与更名前 getDataDir() 的语义一致。
 *
 * 注意：日志目录与配置目录历史上**不读**该覆盖（沙盒隔离契约依赖此行为），
 * 因此它们走 platformAppDataRootForBase，不要改这里去统一。
 */
export function appDataRootForBase(baseDir: string, env: NodeJS.ProcessEnv = process.env): string {
  const override = (env[DATA_DIR_ENV] || '').trim();
  if (override) return path.resolve(override);
  return platformAppDataRootForBase(baseDir);
}

/** 当前平台的数据根目录（不含 data/logs/config）。 */
export function appDataRoot(): string {
  return appDataRootForBase(platformBaseDir());
}

/** 平台默认数据根目录（不读 env 覆盖）：logs/config 的定位基准。 */
export function platformAppDataRoot(): string {
  return platformAppDataRootForBase(platformBaseDir());
}

let migrationDone = false;

/**
 * 主进程启动时显式调用一次：把更名前遗留的 RAGStudyAssistant 根目录迁移为
 * StudyTool。幂等；失败只回退，不抛错。env 覆盖（测试/沙盒）时跳过。
 */
export function ensureAppDataMigration(): void {
  if (migrationDone) return;
  migrationDone = true;
  if ((process.env[DATA_DIR_ENV] || '').trim()) return;
  migrateLegacyRoot(platformBaseDir());
}

/** 后端数据库与上传文件目录。 */
export function dataDir(): string {
  return path.join(appDataRoot(), 'data');
}

/** 桌面与后端日志目录。历史上不读数据目录 env 覆盖（沙盒隔离契约）。 */
export function logDir(): string {
  return path.join(platformAppDataRoot(), 'logs');
}

/** 凭据、外观偏好等主进程配置目录。同样不读数据目录 env 覆盖。 */
export function configDir(): string {
  return path.join(platformAppDataRoot(), 'config');
}
