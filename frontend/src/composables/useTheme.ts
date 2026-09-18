import { ref, computed, readonly, type ComputedRef } from 'vue';
import type { ThemePreference, EffectiveTheme } from './useDesktop';

export type { ThemePreference, EffectiveTheme };

/**
 * 统一主题状态（T1/T3）。
 *
 * - 偏好三态：`system`（默认，跟随系统）/ `light` / `dark`；对外另暴露 `effective` 实际主题。
 * - 真源是 `<html data-theme>` + `color-scheme`，首屏前由 index.html 内联脚本或桌面 preload 先写入，
 *   本模块接管后只在这两处保持一致，不做整页刷新。
 * - 网页持久化在 localStorage（同源多标签页经 `storage` 事件同步）；桌面端以主进程偏好为准
 *   （受限 IPC 读写独立偏好文件），不混用网页存储，避免互相覆盖。
 * - 存储不可用 / 保存失败不阻塞切换：`persistError` 如实上报，由界面提示「未持久保存」。
 */

export const THEME_STORAGE_KEY = 'studytool:theme';
const VALID: readonly ThemePreference[] = ['system', 'light', 'dark'];

/** desktopAPI 的完整类型在 useDesktop.ts 中统一声明，此处不复述（全局声明冲突）。 */

const preference = ref<ThemePreference>('system');
const effective = ref<EffectiveTheme>('light');
/** 最近一次持久化失败的说明；为空表示无错误或尚未尝试。 */
const persistError = ref('');
/** 偏好来源是否已就绪（桌面端等待 preload/IPC 回值）。 */
const ready = ref(false);

const isDesktopTheme = (): boolean =>
  typeof window !== 'undefined' && Boolean(window.desktopAPI?.isDesktop && window.desktopAPI?.setThemePreference);

function normalize(raw: unknown): ThemePreference {
  return typeof raw === 'string' && (VALID as string[]).includes(raw) ? (raw as ThemePreference) : 'system';
}

function systemTheme(): EffectiveTheme {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return 'light';
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

/** 唯一的落点：根元素 data-theme + color-scheme。 */
function applyTheme(next: EffectiveTheme): void {
  if (typeof document === 'undefined') return;
  effective.value = next;
  document.documentElement.dataset.theme = next;
  document.documentElement.style.colorScheme = next;
}

function resolveEffective(pref: ThemePreference): EffectiveTheme {
  return pref === 'system' ? systemTheme() : pref;
}

function readStoredPreference(): ThemePreference {
  try {
    return normalize(window.localStorage.getItem(THEME_STORAGE_KEY));
  } catch {
    return 'system';
  }
}

function writeStoredPreference(pref: ThemePreference): boolean {
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, pref);
    return true;
  } catch {
    return false;
  }
}

let initialized = false;

function init(): void {
  if (initialized || typeof window === 'undefined') return;
  initialized = true;

  if (isDesktopTheme()) {
    // preload 已在首屏前把 initialTheme 写到 DOM 上；这里取回同一真源。
    const initial = window.desktopAPI?.initialTheme;
    preference.value = normalize(initial?.preference);
    // effective 以主进程宣告为准（与窗口背景/nativeTheme 同源）。
    applyTheme(initial?.effective === 'dark' || initial?.effective === 'light' ? initial.effective : resolveEffective(preference.value));
    window.desktopAPI?.onThemeChanged?.((t) => {
      preference.value = normalize(t?.preference);
      applyTheme(t?.effective === 'dark' || t?.effective === 'light' ? t.effective : resolveEffective(preference.value));
      persistError.value = '';
    });
    ready.value = true;
  } else {
    preference.value = readStoredPreference();
    applyTheme(resolveEffective(preference.value));
    ready.value = true;
  }

  // 系统主题变化：仅「跟随系统」时生效；手动选择不被系统覆盖。
  const media = window.matchMedia?.('(prefers-color-scheme: dark)');
  media?.addEventListener?.('change', () => {
    if (preference.value === 'system') applyTheme(systemTheme());
  });

  // 同源多标签页同步（仅网页；桌面端走 onThemeChanged 广播）。
  window.addEventListener('storage', (e) => {
    if (isDesktopTheme()) return;
    if (e.key === THEME_STORAGE_KEY) {
      preference.value = normalize(e.newValue);
      applyTheme(resolveEffective(preference.value));
    }
  });
}

export interface UseTheme {
  preference: Readonly<typeof preference>;
  effective: Readonly<typeof effective>;
  ready: Readonly<typeof ready>;
  persistError: Readonly<typeof persistError>;
  isDesktopTheme: ComputedRef<boolean>;
  setPreference(pref: ThemePreference): Promise<{ ok: boolean; message?: string }>;
}

export function useTheme(): UseTheme {
  init();

  const setPreference = async (pref: ThemePreference): Promise<{ ok: boolean; message?: string }> => {
    const next = normalize(pref);
    preference.value = next;

    if (isDesktopTheme()) {
      // 先按本地解析即时生效，再由主进程回执校准（同一次切换不会闪动）。
      applyTheme(resolveEffective(next));
      try {
        const res = await window.desktopAPI!.setThemePreference!(next);
        if (!res?.ok) {
          persistError.value = res?.message || '外观偏好未能保存';
          return { ok: false, message: persistError.value };
        }
        preference.value = normalize(res.preference);
        applyTheme(res.effective === 'dark' || res.effective === 'light' ? res.effective : resolveEffective(next));
        persistError.value = '';
        return { ok: true };
      } catch (err: any) {
        persistError.value = err?.message || '外观偏好未能保存';
        return { ok: false, message: persistError.value };
      }
    }

    applyTheme(resolveEffective(next));
    if (!writeStoredPreference(next)) {
      persistError.value = '当前浏览器无法保存外观偏好，刷新后将失效';
      return { ok: false, message: persistError.value };
    }
    persistError.value = '';
    return { ok: true };
  };

  return {
    preference: readonly(preference),
    effective: readonly(effective),
    ready: readonly(ready),
    persistError: readonly(persistError),
    isDesktopTheme: computed(isDesktopTheme),
    setPreference,
  };
}
