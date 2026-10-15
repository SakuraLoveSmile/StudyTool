import fs from 'fs';
import path from 'path';
import { configDir as appConfigDir } from './app-paths';

// electron.nativeTheme 只能在 Electron 主进程内使用；node:test 里 require('electron')
// 只返回二进制路径字符串。与 credentials-store 的 safeStorage 同款懒加载。
type NativeThemeLike = { shouldUseDarkColors: boolean };
function getNativeTheme(): NativeThemeLike | null {
  try {
    const electron = require('electron');
    return electron?.nativeTheme ?? null;
  } catch {
    return null;
  }
}

/**
 * 外观偏好（T3）：主进程独立保存，文件与模型密钥、学习数据分开放置。
 *
 * - 真源：`<configDir>/theme.json`，值为 'system' | 'light' | 'dark'；
 * - nativeTheme.themeSource 与之保持一致：原生标题栏、系统对话框、 prefers-color-scheme
 *   媒体查询全部获得同一结论，网页侧无需再做平台特判；
 * - 读取失败 / 非法值一律回退 'system'（默认跟随系统）；
 * - 写入沿用项目惯例：临时文件 + rename 原子替换，避免半截文件。
 */

export type ThemePreference = 'system' | 'light' | 'dark';
export type EffectiveTheme = 'light' | 'dark';

export interface ThemePayload {
  preference: ThemePreference;
  effective: EffectiveTheme;
}

const VALID_PREFERENCES: readonly ThemePreference[] = ['system', 'light', 'dark'];

export function normalizeThemePreference(raw: unknown): ThemePreference {
  return typeof raw === 'string' && (VALID_PREFERENCES as string[]).includes(raw)
    ? (raw as ThemePreference)
    : 'system';
}

export function resolveEffectiveTheme(
  pref: ThemePreference,
  isSystemDark: () => boolean = () => getNativeTheme()?.shouldUseDarkColors === true
): EffectiveTheme {
  if (pref === 'system') {
    return isSystemDark() ? 'dark' : 'light';
  }
  return pref;
}

/** 窗口创建前的预渲染背景色：与实际主题一致，避免冷启动闪白。 */
export function windowBackgroundFor(pref: ThemePreference): string {
  return resolveEffectiveTheme(pref) === 'dark' ? '#1c1917' : '#fafaf9';
}

export class ThemePreferenceStore {
  private readonly filePath: string;

  constructor(customConfigDir?: string) {
    const configDir = customConfigDir || appConfigDir();
    fs.mkdirSync(configDir, { recursive: true });
    this.filePath = path.join(configDir, 'theme.json');
  }

  public load(): ThemePreference {
    try {
      if (!fs.existsSync(this.filePath)) return 'system';
      const raw = fs.readFileSync(this.filePath, 'utf-8');
      if (!raw || !raw.trim()) return 'system';
      const parsed = JSON.parse(raw) as Record<string, unknown>;
      return normalizeThemePreference(parsed?.preference);
    } catch {
      return 'system';
    }
  }

  public save(pref: ThemePreference): void {
    const normalized = normalizeThemePreference(pref);
    const jsonStr = JSON.stringify({ preference: normalized });
    const tempPath = `${this.filePath}.tmp.${Date.now()}.${Math.random().toString(36).slice(2, 8)}`;
    try {
      fs.writeFileSync(tempPath, jsonStr, { mode: 0o600 });
      fs.renameSync(tempPath, this.filePath);
    } catch (err) {
      if (fs.existsSync(tempPath)) {
        try {
          fs.unlinkSync(tempPath);
        } catch {
          // ignore cleanup error
        }
      }
      throw new Error(`保存外观偏好失败: ${err}`);
    }
  }
}
