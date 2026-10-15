import { defineConfig, devices } from '@playwright/test';

/**
 * 两种页面形态用同一份前端源码、两个 Vite dev server：
 *  - "unconfigured"（5274）：完全不提供 VITE_FEEDBACK_API_BASE / VITE_FEEDBACK_APP_ID
 *  - "configured"  （5275）：提供两个构建期变量
 *
 * 选定理由：要验的是**真实构建期变量路径**（`import.meta.env.VITE_FEEDBACK_*`），
 * 不是测试替身。Vite 会把进程环境里 `VITE_` 前缀的变量暴露为 `import.meta.env`，
 * 因此用 webServer 的 `env` 传参即可，**无需改动 frontend/vite.config.ts 或任何产品文件**。
 *
 * 依赖独立：本目录自带 package.json/node_modules；@feedback/web 只读引用
 * frontend/vendor 的 tarball。不写 frontend/package.json、package-lock.json、vendor/。
 */

const FRONTEND_DIR = '../frontend';

export default defineConfig({
  testDir: './specs',
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  workers: 1,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  reporter: [['list'], ['html', { open: 'never', outputFolder: 'playwright-report' }]],
  use: {
    // 复用全局缓存的 chromium-1243（@playwright/test 1.63.0 的 revision 正好是 1243）。
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    serviceWorkers: 'block',
    actionTimeout: 15_000,
  },
  projects: [
    {
      name: 'unconfigured',
      use: { ...devices['Desktop Chrome'], baseURL: 'http://127.0.0.1:5274' },
      testMatch: /specs\/unconfigured\.spec\.ts/,
    },
    {
      name: 'theme',
      use: { ...devices['Desktop Chrome'], baseURL: 'http://127.0.0.1:5274' },
      testMatch: /specs\/theme\.spec\.ts/,
    },
    {
      name: 'configured',
      use: { ...devices['Desktop Chrome'], baseURL: 'http://127.0.0.1:5275' },
      testMatch: /specs\/host-integration\.spec\.ts/,
    },
    {
      // 反馈日志附件（T3b）：同样的已配置形态 dev server（5275），
      // 桌面 API 由各用例经 addInitScript/exposeFunction 注入桩。
      name: 'configured-logs',
      use: { ...devices['Desktop Chrome'], baseURL: 'http://127.0.0.1:5275' },
      testMatch: /specs\/feedback-logs\.spec\.ts/,
    },
    {
      // T4 验收（Windows 候选包前的浏览器级门禁）：导航点击链路与日志页交互。
      name: 'acceptance',
      use: { ...devices['Desktop Chrome'], baseURL: 'http://127.0.0.1:5274' },
      testMatch: /specs\/windows-acceptance\.spec\.ts/,
    },
    {
      name: 'component',
      use: { ...devices['Desktop Chrome'] },
      testMatch: /specs\/component-level\.spec\.ts/,
    },
    {
      // STU-3 资料页回归：材料/任务 API 全部由 spec 内 route 拦截 mock，
      // 用未配置形态 dev server（5274），不发真实 OCR/反馈请求。
      name: 'materials-ocr',
      use: { ...devices['Desktop Chrome'], baseURL: 'http://127.0.0.1:5274' },
      testMatch: /specs\/materials-ocr\.spec\.ts/,
    },
  ],
  webServer: [
    {
      command: 'npm run dev -- --port 5274 --strictPort',
      cwd: FRONTEND_DIR,
      url: 'http://127.0.0.1:5274',
      // 这三个服务都是**无状态**的（Vite dev server / 静态 fixture 服务器），
      // 且 5274/5275 两个 dev server 的构建期变量由下面的 env 固定，复用同一实例不会
      // 改变被测形态。设 true 的目的：只要有一个残留服务占着端口，reuseExistingServer:false
      // 会让 Playwright 直接放弃**整轮**（实测：残留 fixture server 占 5276 → 一次
      // "already used" 导致 7 个用例连锁失败）。宁可复用也不要在本地被残留进程打断。
      reuseExistingServer: true,
      timeout: 120_000,
      stdout: 'ignore',
      stderr: 'pipe',
      // 未配置形态：**必须显式声明空值**。
      // frontend/.env.local（本地开发用，已 gitignore）会把两个变量填成真实值，
      // 而 Vite 的 loadEnv 里 process.env 优先于 .env* 文件 —— 空字符串正好覆盖它。
      // 实测：空值 → 1842 modules、无 feedback/html2canvas chunk（真正的未配置形态）；
      //       不声明 → 走 .env.local 的 1844 modules + 两个 chunk；
      //       `--mode test` 也无效（仍 1844 modules），所以这里不用 --mode。
      env: {
        VITE_FEEDBACK_API_BASE: '',
        VITE_FEEDBACK_APP_ID: '',
      },
    },
    {
      command: 'npm run dev -- --port 5275 --strictPort',
      cwd: FRONTEND_DIR,
      url: 'http://127.0.0.1:5275',
      // 这三个服务都是**无状态**的（Vite dev server / 静态 fixture 服务器），
      // 且 5274/5275 两个 dev server 的构建期变量由下面的 env 固定，复用同一实例不会
      // 改变被测形态。设 true 的目的：只要有一个残留服务占着端口，reuseExistingServer:false
      // 会让 Playwright 直接放弃**整轮**（实测：残留 fixture server 占 5276 → 一次
      // "already used" 导致 7 个用例连锁失败）。宁可复用也不要在本地被残留进程打断。
      reuseExistingServer: true,
      timeout: 120_000,
      stdout: 'ignore',
      stderr: 'pipe',
      env: {
        VITE_FEEDBACK_API_BASE: 'http://127.0.0.1:5199',
        VITE_FEEDBACK_APP_ID: 'studytool',
      },
    },
    {
      // 组件级 fixture 静态服务器（只服务 e2e/fixtures 与 e2e/node_modules/@feedback/web/dist）
      command: 'node helpers/fixture-server.mjs',
      url: 'http://127.0.0.1:5276/widget-fixture.html',
      // 这三个服务都是**无状态**的（Vite dev server / 静态 fixture 服务器），
      // 且 5274/5275 两个 dev server 的构建期变量由下面的 env 固定，复用同一实例不会
      // 改变被测形态。设 true 的目的：只要有一个残留服务占着端口，reuseExistingServer:false
      // 会让 Playwright 直接放弃**整轮**（实测：残留 fixture server 占 5276 → 一次
      // "already used" 导致 7 个用例连锁失败）。宁可复用也不要在本地被残留进程打断。
      reuseExistingServer: true,
      timeout: 30_000,
      stdout: 'ignore',
      stderr: 'pipe',
    },
  ],
});
