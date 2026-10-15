import type { FeedbackLogFile, FeedbackLogProvider } from '@feedback/web';

/**
 * 桌面受限日志采集 → 反馈组件 `logProvider` 的适配层。
 *
 * 职责刻意保持很窄：
 *  - 只读取 preload 注入的受限 IPC 方法 `desktopAPI.getFeedbackLogs`（本文件自带窄接口，
 *    不依赖 useDesktop.ts 的 window.desktopAPI 类型声明——那一份由主代理统一补齐）；
 *  - 把冻结 IPC 契约的响应翻译成组件要的 `{ filename, blob }[]`；
 *  - 失败一律抛**简短中文** Error（组件把 message 直接显示在面板日志错误区），
 *    绝不照搬后端 message（防路径 / 内部信息泄露），也绝不把失败伪装成空成功；
 *  - 不调用 exportDiagnostics（那是 ZIP 诊断导出，与 JSONL 行式日志无关），不自动上传。
 */

/** —— 冻结 IPC 契约：与 preload / 主进程逐字一致，改动必须三方同步 —— */
export type FeedbackLogSource = 'desktop' | 'backend' | 'frontend';
export type FeedbackLogFilename =
  | 'studytool-desktop.jsonl'
  | 'studytool-backend.jsonl'
  | 'studytool-frontend.jsonl';
export type FeedbackLogsResult =
  | {
      ok: true;
      files: Array<{ filename: FeedbackLogFilename; text: string }>;
      truncated: boolean;
      unavailable: FeedbackLogSource[];
      skippedLines: number;
    }
  | {
      ok: false;
      code:
        | 'LOG_FEEDBACK_BUSY'
        | 'LOG_FEEDBACK_TIMEOUT'
        | 'LOG_FEEDBACK_CANCELED'
        | 'LOG_FEEDBACK_UNAVAILABLE'
        | 'LOG_FEEDBACK_FAILED';
      message: string;
    };

/**
 * 本模块自有的窄接口：与 useDesktop.ts 中 window.desktopAPI 的
 * `getFeedbackLogs` 声明同构，这里只按结构取用；方法缺失或不是函数一律视为「通道不可用」。
 */
export interface FeedbackLogsDesktopApi {
  getFeedbackLogs(): Promise<FeedbackLogsResult>;
}

/** 组件上限 3 个 / 单文件 1MiB；宿主侧更严：单文件 ≤256KiB。 */
const MAX_LOG_FILES = 3;
const MAX_LOG_FILE_BYTES = 256 * 1024;

const ALLOWED_FILENAMES: ReadonlySet<string> = new Set<FeedbackLogFilename>([
  'studytool-desktop.jsonl',
  'studytool-backend.jsonl',
  'studytool-frontend.jsonl',
]);
const ALLOWED_SOURCES: ReadonlySet<string> = new Set<FeedbackLogSource>(['desktop', 'backend', 'frontend']);

/** 展示给用户的文案统一为简短中文，绝不透传后端 message。 */
const ERR_STRUCTURE = '日志响应结构非法';
const ERR_CHANNEL = '日志采集通道异常';

function failureMessage(code: string): string {
  switch (code) {
    case 'LOG_FEEDBACK_BUSY':
      return '日志采集进行中，请稍后重试';
    case 'LOG_FEEDBACK_TIMEOUT':
      return '日志采集超时';
    case 'LOG_FEEDBACK_CANCELED':
      return '日志采集已取消';
    case 'LOG_FEEDBACK_UNAVAILABLE':
      return '日志不可用';
    case 'LOG_FEEDBACK_FAILED':
    default:
      return '日志采集失败';
  }
}

/**
 * 取受限日志通道。`window.desktopAPI` 由 preload 注入；
 * 浏览器形态 / 旧 preload / 方法被篡改（非函数）时都返回 null —— 调用方不安装 provider。
 */
export function resolveFeedbackLogsApi(): FeedbackLogsDesktopApi | null {
  if (typeof window === 'undefined') return null;
  const api = window.desktopAPI as unknown as { getFeedbackLogs?: unknown } | undefined;
  const fn = api?.getFeedbackLogs;
  if (typeof fn !== 'function') return null;
  const call = fn as (this: unknown) => Promise<FeedbackLogsResult>;
  return { getFeedbackLogs: () => call.call(api) };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function isAllowedSource(value: unknown): value is FeedbackLogSource {
  return typeof value === 'string' && ALLOWED_SOURCES.has(value);
}

/**
 * 契约响应 → 组件附件列表。
 *
 * 两层校验：
 *  1. 结构校验：ok / files / truncated / unavailable / skippedLines 及每个文件项的
 *     { filename: string, text: string } 形态。结构非法 → throw（面板显示 ERR_STRUCTURE）；
 *  2. 逐文件校验：文件名三值白名单、不重复、总数 ≤3、UTF-8 字节 ≤256KiB、非空。
 *     违规文件**丢弃**（不报错）；最终零有效文件 → 返回 []（无附件，不是错误）。
 */
function toFeedbackLogFiles(result: unknown): FeedbackLogFile[] {
  if (!isRecord(result) || typeof result.ok !== 'boolean') {
    throw new Error(ERR_STRUCTURE);
  }
  if (result.ok === false) {
    if (typeof result.code !== 'string' || typeof result.message !== 'string') {
      throw new Error(ERR_STRUCTURE);
    }
    throw new Error(failureMessage(result.code));
  }
  if (
    !Array.isArray(result.files) ||
    typeof result.truncated !== 'boolean' ||
    !Array.isArray(result.unavailable) ||
    !result.unavailable.every(isAllowedSource) ||
    typeof result.skippedLines !== 'number' ||
    !Number.isFinite(result.skippedLines)
  ) {
    throw new Error(ERR_STRUCTURE);
  }
  for (const item of result.files) {
    if (!isRecord(item) || typeof item.filename !== 'string' || typeof item.text !== 'string') {
      throw new Error(ERR_STRUCTURE);
    }
  }

  const out: FeedbackLogFile[] = [];
  const seen = new Set<string>();
  for (const item of result.files) {
    if (out.length >= MAX_LOG_FILES) break;
    if (!ALLOWED_FILENAMES.has(item.filename) || seen.has(item.filename)) continue;
    const blob = new Blob([item.text], { type: 'text/plain;charset=utf-8' });
    // 空文件组件必然拒收（"不能为空文件"），超限按宿主更严规则丢弃。
    if (blob.size === 0 || blob.size > MAX_LOG_FILE_BYTES) continue;
    seen.add(item.filename);
    out.push({ filename: item.filename, blob });
  }
  return out;
}

export interface FeedbackLogProviderDeps {
  /** 受限 IPC 通道（由 resolveFeedbackLogsApi 取得）。 */
  api: FeedbackLogsDesktopApi;
  /**
   * 实例存活判定：useFeedbackWidget 绑定到具体组件实例。
   * 实例销毁 / 被替换后，旧 provider 迟到的结果一律作废为空数组——
   * 宿主侧先挡一道；组件自身的 epoch / draft / opSeq 校验再挡一道。
   */
  isAlive: () => boolean;
}

/**
 * 返回组件 `logProvider` 形态的函数：
 *  - IPC 通道断开 / 调用 reject → throw 简短中文 Error；
 *  - 销毁后调用或销毁后返回 → resolve []（不产生任何附件）；
 *  - 正常失败（ok:false）→ throw 按 code 映射的中文 Error；
 *  - 成功 → resolve 校验后的 { filename, blob }[]（可能为空数组）。
 */
export function createFeedbackLogProvider(deps: FeedbackLogProviderDeps): FeedbackLogProvider {
  return async () => {
    if (!deps.isAlive()) return [];
    let result: unknown;
    try {
      result = await deps.api.getFeedbackLogs();
    } catch {
      throw new Error(ERR_CHANNEL);
    }
    if (!deps.isAlive()) return [];
    return toFeedbackLogFiles(result);
  };
}
