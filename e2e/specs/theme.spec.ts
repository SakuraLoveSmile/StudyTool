import { expect, test, type Page } from '@playwright/test';
import { HOST_USER_A, mockHostApi } from '../helpers/host-page';

/**
 * 深色模式自动化验收（T4，运行在 5274 未配置形态——主题与反馈配置无关）。
 *
 * 真源断言一律读 `<html data-theme>`：这是首屏脚本、useTheme 与全部语义 token
 * 共同依赖的唯一落点；读它等价于读「用户实际看到的主题」。
 * 系统主题用 page.emulateMedia 模拟，覆盖 matchMedia 监听路径。
 */

const THEME_KEY = 'studytool:theme';

async function themeOf(page: Page): Promise<string | undefined> {
  return page.evaluate(() => document.documentElement.dataset.theme);
}

/** 在页面脚本执行前写入（或篡改）localStorage 中的偏好。 */
async function seedStoredTheme(page: Page, value: string) {
  await page.addInitScript(
    ({ key, v }) => {
      try {
        window.localStorage.setItem(key, v);
      } catch {
        /* 与产品代码同一假设：存储不可用时按无偏好处理 */
      }
    },
    { key: THEME_KEY, v: value }
  );
}

/** 通过导航栏/登录页同款入口选择主题。 */
async function chooseThemeViaToggle(page: Page, label: '跟随系统' | '浅色' | '深色') {
  await page.getByRole('button', { name: /外观设置/ }).click();
  await page.getByRole('menuitemradio', { name: label }).click();
}

/** 装好宿主 API mock 的已登录外壳。 */
async function openAuthedApp(page: Page, path = '/today') {
  const state = { user: HOST_USER_A as typeof HOST_USER_A | null, csrfToken: null as string | null };
  await mockHostApi(page, state);
  await page.route(/\/api\/desktop\/status(\?|$)/, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ idle: true, active_tasks: 0, has_existing_embeddings: false, desktop_mode: false }),
    })
  );
  await page.goto(path);
  return state;
}

test.describe('初始化与跟随系统', () => {
  test('首次使用默认跟随系统', async ({ page }) => {
    await page.emulateMedia({ colorScheme: 'dark' });
    await page.goto('/login');
    expect(await themeOf(page)).toBe('dark');

    await page.emulateMedia({ colorScheme: 'light' });
    await page.goto('/login');
    expect(await themeOf(page)).toBe('light');
  });

  test('非法偏好回退跟随系统', async ({ page }) => {
    await seedStoredTheme(page, 'bogus-value');
    await page.emulateMedia({ colorScheme: 'light' });
    await page.goto('/login');
    expect(await themeOf(page)).toBe('light');
    expect(await page.evaluate((k) => localStorage.getItem(k), THEME_KEY)).toBe('bogus-value');
  });

  test('跟随系统时系统主题变化自动更新，不刷新页面', async ({ page }) => {
    await page.emulateMedia({ colorScheme: 'light' });
    await page.goto('/login');
    expect(await themeOf(page)).toBe('light');

    // 不刷新页面直接翻转系统主题：matchMedia change → data-theme 跟随
    await page.emulateMedia({ colorScheme: 'dark' });
    await expect.poll(() => themeOf(page)).toBe('dark');
    await page.emulateMedia({ colorScheme: 'light' });
    await expect.poll(() => themeOf(page)).toBe('light');
  });

  test('手动选择后不再被系统覆盖', async ({ page }) => {
    await page.emulateMedia({ colorScheme: 'light' });
    await page.goto('/login');
    await chooseThemeViaToggle(page, '深色');
    expect(await themeOf(page)).toBe('dark');

    await page.emulateMedia({ colorScheme: 'light' });
    await page.waitForTimeout(150); // 给 matchMedia 监听器留出误触发窗口
    expect(await themeOf(page)).toBe('dark');
  });
});

test.describe('切换与持久化', () => {
  test('选择立即生效，刷新后保留', async ({ page }) => {
    await page.emulateMedia({ colorScheme: 'light' });
    await page.goto('/login');
    await chooseThemeViaToggle(page, '深色');
    expect(await themeOf(page)).toBe('dark');
    expect(await page.evaluate((k) => localStorage.getItem(k), THEME_KEY)).toBe('dark');

    await page.reload();
    // 首屏脚本在绘制前写入：读到 dark 即证明没有先画浅色再翻转。
    expect(await themeOf(page)).toBe('dark');
  });

  test('三态可往返：深色 → 浅色 → 跟随系统', async ({ page }) => {
    await page.emulateMedia({ colorScheme: 'dark' });
    await page.goto('/login');

    await chooseThemeViaToggle(page, '浅色');
    expect(await themeOf(page)).toBe('light');

    await chooseThemeViaToggle(page, '深色');
    expect(await themeOf(page)).toBe('dark');

    await chooseThemeViaToggle(page, '跟随系统');
    expect(await themeOf(page)).toBe('dark'); // 系统为 dark
    await page.emulateMedia({ colorScheme: 'light' });
    await expect.poll(() => themeOf(page)).toBe('light');
  });

  test('切换不刷新页面、不丢失输入内容', async ({ page }) => {
    await page.goto('/login');
    const username = page.locator('#login-username');
    await username.fill('keep-me');
    await chooseThemeViaToggle(page, '深色');
    expect(await themeOf(page)).toBe('dark');
    await expect(username).toHaveValue('keep-me');
  });

  test('同源多标签页同步变化', async ({ context, page }) => {
    await page.emulateMedia({ colorScheme: 'light' });
    await page.goto('/login');
    const page2 = await context.newPage();
    await page2.goto('/login');

    await chooseThemeViaToggle(page, '深色');
    await expect.poll(() => themeOf(page2)).toBe('dark');

    await chooseThemeViaToggle(page2, '浅色');
    await expect.poll(() => themeOf(page)).toBe('light');
  });

  test('存储不可用时仍可切换，刷新后如实回到系统主题', async ({ page }) => {
    await page.emulateMedia({ colorScheme: 'light' });
    await page.addInitScript(() => {
      Object.defineProperty(window, 'localStorage', {
        configurable: true,
        get() {
          throw new Error('denied');
        },
      });
    });
    await page.goto('/login');
    expect(await themeOf(page)).toBe('light');

    await chooseThemeViaToggle(page, '深色');
    expect(await themeOf(page)).toBe('dark');

    await page.reload();
    expect(await themeOf(page)).toBe('light');
  });

  test('退出登录后外观保留', async ({ page }) => {
    await page.emulateMedia({ colorScheme: 'light' });
    await openAuthedApp(page, '/today');
    await chooseThemeViaToggle(page, '深色');
    expect(await themeOf(page)).toBe('dark');

    await page.getByRole('button', { name: '退出' }).click();
    await page.waitForURL(/login/);
    expect(await themeOf(page)).toBe('dark');
  });
});

test.describe('设置页「外观」区', () => {
  test('独立区域出现且选择立即生效', async ({ page }) => {
    await page.emulateMedia({ colorScheme: 'light' });
    await openAuthedApp(page, '/settings');

    const group = page.getByRole('radiogroup', { name: '外观' });
    await expect(group).toBeVisible();
    await expect(page.getByRole('radio', { name: '跟随系统' })).toHaveAttribute('aria-checked', 'true');

    await page.getByRole('radio', { name: '深色' }).click();
    expect(await themeOf(page)).toBe('dark');
    await expect(page.getByRole('radio', { name: '深色' })).toHaveAttribute('aria-checked', 'true');
  });
});
