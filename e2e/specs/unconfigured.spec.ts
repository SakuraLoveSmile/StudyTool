import { expect, test, type Page } from '@playwright/test';
import { HOST_USER_A, mockHostApi, type HostState } from '../helpers/host-page';

/**
 * F1：未配置构建不出现反馈入口。
 *
 * **前提必须被证明，不能被假设。** 仓库根下存在 `frontend/.env.local`（本地开发用，已 gitignore），
 * 它会把两个 `VITE_FEEDBACK_*` 变量填成真实值。Vite 的 `loadEnv` 里 `process.env` 优先于
 * `.env*` 文件，所以未配置形态靠 `webServer[].env` 显式声明**空字符串**
 * （`VITE_FEEDBACK_API_BASE: ''` / `VITE_FEEDBACK_APP_ID: ''`）来保证。
 *
 * 注意：**`--mode test` 不是可行替代** —— 实测仍会走 1844 modules 并产出
 * feedback / html2canvas 两个 chunk，因为 `loadEnv` 读到的是 `.env.local` 的实际值。
 *
 * 因此两个用例都先跑 `assertUnconfiguredForm`：若 `.env.local` 重新生效（或有人挪走
 * webServer 的 env 声明），前提会**直接、可读地失败**，而不是让后面的否定式断言
 * 因为「什么都没加载」而空过。
 */

/** 组件包与懒加载截图依赖的请求特征。
 *  覆盖两种命名：Vite 预打包的 `@feedback_web.js` 与产物里的 `feedback-web-*.js`。 */
const FEEDBACK_ASSET = /@?feedback[-_]web|html2canvas/i;

async function assertUnconfiguredForm(page: Page, requested: string[]): Promise<void> {
  // 1) 应用外壳确实渲染了：证明「什么都没加载」不是页面没起来的假象。
  await expect(page.locator('nav'), '应用外壳应已渲染').toBeVisible();
  await expect(page.locator('main'), '主内容区应已渲染').toBeVisible();

  // 2) 给出等待窗口，让「本应发生却没有发生」的请求/挂载有机会暴露。
  //    配置生效时组件包是 mounted 时动态导入的，1.5s 足够触发。
  await page.waitForTimeout(1500);

  // 3) 前提：未配置形态 = 没有反馈入口。
  await expect(
    page.locator('feedback-widget'),
    '前提不成立：当前不是未配置形态（出现了 feedback-widget 实例）。' +
      '很可能是 frontend/.env.local 的 VITE_FEEDBACK_* 生效了 —— ' +
      '未配置形态必须在 playwright.config.ts 的 unconfigured webServer.env 里显式声明空值。',
  ).toHaveCount(0);

  // 4) 前提：未配置形态 = 组件包从未被动态导入。
  const assets = requested.filter((url) => FEEDBACK_ASSET.test(url));
  expect(
    assets,
    '前提不成立：当前不是未配置形态（组件包/截图依赖已被请求）。' +
      '最可能是 frontend/.env.local 覆盖了空值；不要改用 --mode（实测无效），' +
      `必须在 webServer.env 显式声明空字符串。实际请求：${assets.join(', ')}`,
  ).toEqual([]);

  // 5) 未注册自定义元素 = 动态导入确实没发生（比只看 DOM 更硬）。
  const defined = await page.evaluate(() => Boolean(customElements.get('feedback-widget')));
  expect(
    defined,
    '前提不成立：feedback-widget 自定义元素已注册，说明组件包被加载了（不是未配置形态）。',
  ).toBe(false);
}

test.describe('未配置形态（F1）', () => {
  test('不出现反馈入口，且组件包从未被请求', async ({ page }) => {
    const state: HostState = { user: HOST_USER_A, csrfToken: null };
    await mockHostApi(page, state);

    const requested: string[] = [];
    page.on('request', (request) => requested.push(request.url()));

    await page.goto('/');

    // 前置断言：先证明「当前确是未配置形态」，否则后续否定式断言会空过。
    await assertUnconfiguredForm(page, requested);

    // 下方的具体断言在前提成立后才有意义。
    await expect(page.locator('nav')).toBeVisible();
    await expect(page.locator('footer')).toBeVisible();
    // 会话已就绪（外壳渲染）后仍不应有反馈入口。
    await expect(page.locator('feedback-widget')).toHaveCount(0);
    await expect(page.locator('#app feedback-widget')).toHaveCount(0);
    // 未配置时不注册自定义元素（动态导入未发生）。
    expect(await page.evaluate(() => Boolean(customElements.get('feedback-widget')))).toBe(false);
  });

  test('学习页面在未配置形态下正常工作（行为与接入前一致）', async ({ page }) => {
    const state: HostState = { user: HOST_USER_A, csrfToken: null };
    await mockHostApi(page, state);

    const requested: string[] = [];
    page.on('request', (request) => requested.push(request.url()));

    await page.goto('/');

    // 同样先确立前提：未配置形态下才谈「行为与接入前一致」。
    await assertUnconfiguredForm(page, requested);

    // 外壳与主导航可见、路由为 today、无任何反馈实例。
    await expect(page.locator('nav')).toBeVisible();
    await expect(page).toHaveURL(/\/today$/);
    await expect(page.locator('feedback-widget')).toHaveCount(0);
    // 主内容区仍可正常渲染（学习页面不受影响）。
    await expect(page.locator('main')).toBeVisible();

    // 兜底复查：整轮请求里都没有组件包/截图依赖。
    expect(requested.filter((url) => FEEDBACK_ASSET.test(url))).toEqual([]);
  });
});
