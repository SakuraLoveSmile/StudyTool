// 临时验收运行配置（Tester 勘察用，不进入产品接线）。
// playwright.config.ts 的正式 project 接线由主代理在集成时补；
// 本文件只为在本机验证 specs/windows-acceptance.spec.ts 能真实跑通。
// 用法：cd e2e && npx playwright test --config ../.devin/windows-logs-capture-fix/playwright.acceptance.config.mjs
import { defineConfig, devices } from '/Users/sakurasep/Documents/Code/Project/Personal Project/StudyTool/e2e/node_modules/@playwright/test/index.mjs';

const REPO = '/Users/sakurasep/Documents/Code/Project/Personal Project/StudyTool';

export default defineConfig({
  testDir: `${REPO}/e2e/specs`,
  testMatch: /windows-acceptance\.spec\.ts/,
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  workers: 1,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  reporter: [['list']],
  use: {
    ...devices['Desktop Chrome'],
    baseURL: 'http://127.0.0.1:5280',
    serviceWorkers: 'block',
    actionTimeout: 15_000,
  },
  projects: [
    {
      name: 'desktop-stub',
      use: { ...devices['Desktop Chrome'], baseURL: 'http://127.0.0.1:5280' },
    },
  ],
  webServer: [
    {
      command: 'npm run dev -- --port 5280 --strictPort',
      cwd: `${REPO}/frontend`,
      url: 'http://127.0.0.1:5280',
      reuseExistingServer: true,
      timeout: 120_000,
      stdout: 'ignore',
      stderr: 'pipe',
      env: {
        VITE_FEEDBACK_API_BASE: '',
        VITE_FEEDBACK_APP_ID: '',
      },
    },
  ],
});
