import { defineConfig, devices } from '@playwright/test';

/**
 * 今日页「保存反馈」专项配置（/today 的 学习范围 / 今日设置 两张卡片）。
 *
 * 与 playwright.config.ts 完全隔离：
 * - 独立端口 5381、独立报告目录（playwright-report-today-save）与产物目录
 *   （test-results-today-save），不触碰既有 5274/5275 dev server 与 5276 fixture server。
 * - 只跑 specs/today-save-feedback.spec.ts：workers=1、retries=0、forbidOnly。
 *
 * webServer 说明：
 * - `reuseExistingServer: false` + `--strictPort`：端口被占用直接失败，宁可暴露残留进程
 *   也不偷偷复用一个「形态未知」的 dev server。
 * - `VITE_FEEDBACK_API_BASE` / `VITE_FEEDBACK_APP_ID` 显式置空：process.env 优先于
 *   frontend/.env.local，保证反馈组件不挂载 —— 本套件与反馈组件无关，顺带证明
 *   保存反馈不依赖它。
 * - frontend/vite.config.ts 的 /api → http://127.0.0.1:8000 代理不会被触达：
 *   spec 内 page.route 按 pathname 前缀 /api/ 拦截全部 /api 流量，未声明的一律
 *   abort + 记录，因此本套件不需要真实 backend。
 */

const FRONTEND_DIR = '../frontend';
const PORT = 5381;
const BASE_URL = `http://127.0.0.1:${PORT}`;

export default defineConfig({
  testDir: './specs',
  testMatch: /today-save-feedback\.spec\.ts/,
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  workers: 1,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  reporter: [['list'], ['html', { open: 'never', outputFolder: 'playwright-report-today-save' }]],
  // 失败截图 / trace 与 spec 内显式截图共用同一产物目录（Playwright 每轮自行清理）。
  outputDir: 'test-results-today-save',
  use: {
    ...devices['Desktop Chrome'],
    baseURL: BASE_URL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    serviceWorkers: 'block',
    actionTimeout: 15_000,
  },
  webServer: {
    command: `npm run dev -- --port ${PORT} --strictPort`,
    cwd: FRONTEND_DIR,
    url: BASE_URL,
    reuseExistingServer: false,
    timeout: 120_000,
    stdout: 'ignore',
    stderr: 'pipe',
    env: {
      VITE_FEEDBACK_API_BASE: '',
      VITE_FEEDBACK_APP_ID: '',
    },
  },
});
