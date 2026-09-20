import { expect, test, type Page } from '@playwright/test';
import { HOST_USER_A, mockHostApi } from '../helpers/host-page';

/**
 * T4 Windows 验收准备 spec（Tester 独占新文件）。
 *
 * 验证边界（与 .devin/windows-logs-capture-fix/acceptance-checklist.md 对应）：
 *  - 本文件只覆盖「浏览器 + window.desktopAPI 桩」下能真实成立的验收项：
 *    顶部「日志」导航入口、日志页筛选/翻页/刷新/错误态、受限按钮的 IPC 调用面、
 *    data-feedback-capture-mask 的 DOM 级存在性。
 *  - 真实 Electron IPC / preload / Worker 线程、NSIS 安装与升级、截图像素级遮罩、
 *    反馈附件真实字节一律不在此断言 —— 它们需要 Windows 候选包或归 Worker A/D 的
 *    测试覆盖，见文末 test.fixme 占位（禁止假通过）。
 *
 * desktopAPI 桩经 page.addInitScript 在页面脚本之前注入，因此与宿主的
 * VITE_FEEDBACK_* 构建期变量无关：5274（未配置）/5275（已配置）任一 project 都能跑。
 * 宿主 /api/* 仍走 route 拦截，不碰真实 backend。
 */

// ---------------------------------------------------------------- 桩的类型（镜像 useDesktop.ts 的冻结形状，本地声明避免引入产品代码）

interface StubLogEntry {
  timestamp: string;
  timestamp_ms: number;
  level: 'DEBUG' | 'INFO' | 'WARN' | 'ERROR';
  source: 'desktop' | 'backend' | 'frontend';
  event: string;
  message: string;
  session_id: string | null;
  task_id?: string;
  record_id?: string;
}

interface StubFailure {
  code: string;
  message: string;
  retryable?: boolean;
}

interface DesktopStubSeed {
  entries: StubLogEntry[];
  pageSize: number;
  failure: StubFailure | null;
  unavailable?: string[];
}

interface StubCalls {
  queryLogs: Array<Record<string, unknown>>;
  getLogMeta: number;
  openLogDirectory: number;
  exportDiagnostics: Array<Record<string, unknown>>;
  cancelLogQuery: string[];
  getFeedbackLogs: number;
  reportLog: unknown[];
}

const SESSION_ID = 'sess-test-0001';

function makeEntries(count: number): StubLogEntry[] {
  const base = Date.now();
  return Array.from({ length: count }, (_, i) => ({
    timestamp: new Date(base - i * 1000).toISOString(),
    timestamp_ms: base - i * 1000,
    level: i % 4 === 0 ? 'ERROR' : 'INFO',
    source: 'backend',
    event: `task.event.${i}`,
    message: `第 ${i} 条诊断消息`,
    session_id: SESSION_ID,
    task_id: i % 2 === 0 ? `task-${String(i).padStart(4, '0')}` : undefined,
    record_id: `rec-${String(i).padStart(3, '0')}`,
  }));
}

/**
 * 注入 window.desktopAPI 桩。
 *
 * 桩刻意**只模拟 IPC 边界**，不复刻主进程语义：
 *  - queryLogs 在桩内做 levels/sources/sessionId/taskId/keyword 过滤与分页，
 *    用于验证「渲染进程把筛选条件正确编码进受限通道、并把结果正确渲染」；
 *    from/to 只作透传断言（时间窗过滤是主进程/Worker 的职责，Windows 包验收再验）。
 *  - seed.failure 非空时返回冻结的失败结构（ok:false + code/message/retryable），
 *    绝不伪装成「成功的空数组」。测试可经 window.__desktopStub.failure 中途切换。
 *  - getFeedbackLogs 默认返回 LOG_FEEDBACK_UNAVAILABLE：T3b 的 logProvider 适配层
 *    落地后面板打开会自动采集，桩先占位保证调用面存在，行为验证归 Worker D 的 spec。
 */
async function installDesktopStub(page: Page, seed: DesktopStubSeed): Promise<void> {
  await page.addInitScript((s: DesktopStubSeed) => {
    interface W extends Window {
      __desktopStub?: { failure: StubFailure | null; calls: StubCalls };
      desktopAPI?: Record<string, unknown>;
    }
    const w = window as W;
    const state = {
      failure: s.failure,
      calls: {
        queryLogs: [] as Array<Record<string, unknown>>,
        getLogMeta: 0,
        openLogDirectory: 0,
        exportDiagnostics: [] as Array<Record<string, unknown>>,
        cancelLogQuery: [] as string[],
        getFeedbackLogs: 0,
        reportLog: [] as unknown[],
      },
    };
    w.__desktopStub = state;

    const meta = {
      sessionId: 'sess-test-0001',
      appVersion: '9.9.9-stub',
      platform: 'stub',
      arch: 'x64',
      logDir: '/stub/logs',
      sources: ['desktop', 'backend', 'frontend'],
      levels: ['DEBUG', 'INFO', 'WARN', 'ERROR'],
      pageSize: s.pageSize,
      defaultRangeMs: 24 * 60 * 60 * 1000,
      maxRangeMs: 7 * 24 * 60 * 60 * 1000,
    };

    const applyFilters = (params: Record<string, unknown>): StubLogEntry[] => {
      let items = s.entries.slice();
      const levels = Array.isArray(params.levels) ? (params.levels as string[]) : [];
      const sources = Array.isArray(params.sources) ? (params.sources as string[]) : [];
      if (levels.length) items = items.filter((e) => levels.includes(e.level));
      if (sources.length) items = items.filter((e) => sources.includes(e.source));
      if (typeof params.sessionId === 'string' && params.sessionId) {
        items = items.filter((e) => e.session_id === params.sessionId);
      }
      if (typeof params.taskId === 'string' && params.taskId) {
        items = items.filter((e) => e.task_id === params.taskId);
      }
      if (typeof params.keyword === 'string' && params.keyword) {
        const kw = params.keyword;
        items = items.filter((e) => e.event.includes(kw) || e.message.includes(kw));
      }
      return items;
    };

    w.desktopAPI = {
      isDesktop: true,
      // 外观偏好：与真实 preload 同形（system/light），使 isDesktopTheme() 走桌面路径。
      initialTheme: { preference: 'system', effective: 'light' },
      getThemePreference: async () => ({ preference: 'system', effective: 'light' }),
      setThemePreference: async (preference: string) => ({
        ok: true,
        preference,
        effective: preference === 'dark' ? 'dark' : 'light',
      }),
      onThemeChanged: () => () => {},
      getLogMeta: async () => {
        state.calls.getLogMeta += 1;
        return { ok: true, ...meta };
      },
      queryLogs: async (params: Record<string, unknown>) => {
        const snapshot = JSON.parse(JSON.stringify(params ?? {})) as Record<string, unknown>;
        state.calls.queryLogs.push(snapshot);
        const pageNo = Number.isFinite(Number(snapshot.page)) ? Math.max(1, Math.floor(Number(snapshot.page))) : 1;
        const pageSize = Number.isFinite(Number(snapshot.pageSize)) ? Math.floor(Number(snapshot.pageSize)) : s.pageSize;
        if (state.failure) {
          return {
            ok: false,
            entries: [],
            total: 0,
            page: pageNo,
            pageSize,
            hasMore: false,
            scannedBytes: 0,
            scanTruncated: false,
            unavailable: [],
            skippedLines: 0,
            requestId: snapshot.requestId,
            code: state.failure.code,
            message: state.failure.message,
            retryable: state.failure.retryable ?? true,
          };
        }
        const items = applyFilters(snapshot);
        const start = (pageNo - 1) * pageSize;
        return {
          ok: true,
          entries: items.slice(start, start + pageSize),
          total: items.length,
          page: pageNo,
          pageSize,
          hasMore: start + pageSize < items.length,
          scannedBytes: items.length,
          scanTruncated: false,
          unavailable: s.unavailable ?? [],
          skippedLines: 0,
          requestId: snapshot.requestId,
        };
      },
      cancelLogQuery: async (requestId: string) => {
        state.calls.cancelLogQuery.push(requestId);
        return { ok: true, canceled: true };
      },
      openLogDirectory: () => {
        state.calls.openLogDirectory += 1;
      },
      exportDiagnostics: async (params: Record<string, unknown>) => {
        state.calls.exportDiagnostics.push(JSON.parse(JSON.stringify(params ?? {})));
        // 默认走「用户取消」分支：无需文件系统也能验证参数编码与提示文案。
        return { ok: false, canceled: true, message: 'stub: 用户取消导出' };
      },
      getFeedbackLogs: async () => {
        state.calls.getFeedbackLogs += 1;
        return { ok: false, code: 'LOG_FEEDBACK_UNAVAILABLE', message: 'stub: 无可用日志来源' };
      },
      reportLog: async (entries: unknown[]) => {
        state.calls.reportLog.push(entries);
        return { ok: true, accepted: Array.isArray(entries) ? entries.length : 0, dropped: 0 };
      },
      // 日志页不触达但与真实 preload 同形的只读面（防止其他模块读到 undefined）。
      getSystemStatus: async () => ({
        idle: true,
        active_tasks: 0,
        has_existing_embeddings: false,
        desktop_mode: true,
      }),
      onUpdateStatus: () => () => {},
    };
  }, seed);
}

interface DesktopStubHandle {
  calls: StubCalls;
}

async function readStubCalls(page: Page): Promise<StubCalls> {
  return page.evaluate(() => {
    const stub = (window as Window & { __desktopStub?: DesktopStubHandle }).__desktopStub;
    if (!stub) throw new Error('desktopAPI 桩未安装：installDesktopStub 必须在 goto 之前调用');
    return stub.calls;
  });
}

async function lastQueryCall(page: Page): Promise<Record<string, unknown>> {
  const calls = await readStubCalls(page);
  const list = calls.queryLogs;
  if (!list.length) throw new Error('尚无 queryLogs 调用');
  return list[list.length - 1];
}

/** TaskDrawer 挂载即拉任务历史；补一条 mock 让外壳干净渲染（host-page.ts 未覆盖此路径）。 */
async function stubTaskHistory(page: Page): Promise<void> {
  await page.route(/\/api\/materials\/tasks\/history(\?|$)/, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ tasks: [], total: 0, page: 1, limit: 50, active_count: 0, actionable_count: 0, history_count: 0 }),
    }),
  );
}

async function mockShell(page: Page) {
  const state = { user: HOST_USER_A as typeof HOST_USER_A | null, csrfToken: null as string | null };
  await mockHostApi(page, state);
  await stubTaskHistory(page);
  return state;
}

// ================================================================ 可验证项：导航与入口

test.describe('导航与「日志」入口', () => {
  test('从首页点击顶部「日志」链接到达 /logs（不是直接 goto）', async ({ page }) => {
    await installDesktopStub(page, { entries: makeEntries(3), pageSize: 5, failure: null });
    await mockShell(page);

    await page.goto('/');
    await expect(page).toHaveURL(/\/today$/);

    // 入口存在的前提是 window.desktopAPI.isDesktop && queryLogs 同时成立（Navbar isDesktop 门禁）。
    const nav = page.locator('nav[aria-label="主导航"]');
    const logsLink = nav.getByRole('link', { name: '日志', exact: true });
    await expect(logsLink, 'desktopAPI 桩存在时主导航必须显示「日志」入口').toBeVisible();

    await logsLink.click();
    await expect(page).toHaveURL(/\/logs$/, { timeout: 10_000 });
    await expect(page.getByRole('heading', { name: '运行日志' })).toBeVisible();
    await expect(page.getByText(/测试版诊断日志 · 只读/)).toBeVisible();

    // 到达即发起首屏查询（meta 与首查询并行，meta 可能仍在途）。
    await expect.poll(async () => (await readStubCalls(page)).queryLogs.length).toBeGreaterThan(0);
    await expect.poll(async () => (await readStubCalls(page)).getLogMeta).toBeGreaterThan(0);
  });

  test('无 desktopAPI 桩时入口隐藏、/logs 如实提示仅桌面端可用且按钮禁用', async ({ page }) => {
    // 不安装桩：模拟纯浏览器形态。
    await mockShell(page);

    await page.goto('/');
    await expect(page).toHaveURL(/\/today$/);
    const nav = page.locator('nav[aria-label="主导航"]');
    await expect(nav.getByRole('link', { name: '设置' })).toBeVisible(); // 导航已渲染的证据
    await expect(nav.getByRole('link', { name: '日志', exact: true })).toHaveCount(0);

    await page.goto('/logs');
    await expect(page.getByRole('heading', { name: '运行日志' })).toBeVisible();
    await expect(
      page.getByText('日志查看与导出仅在桌面客户端可用。浏览器版本不提供日志入口，也不开放 HTTP 日志接口。'),
    ).toBeVisible();
    // 受限按钮在浏览器形态下必须禁用（不是隐藏后假装能用）。
    await expect(page.getByRole('button', { name: /打开日志目录/ })).toBeDisabled();
    await expect(page.getByRole('button', { name: /导出诊断包/ })).toBeDisabled();
  });
});

// ================================================================ 可验证项：日志页交互

test.describe('日志页在 desktopAPI 桩下的交互', () => {
  test('筛选、翻页与手动刷新把正确参数送进受限通道', async ({ page }) => {
    await installDesktopStub(page, { entries: makeEntries(12), pageSize: 5, failure: null });
    await mockShell(page);
    await page.goto('/logs');

    await expect(page.getByRole('heading', { name: '运行日志' })).toBeVisible();
    await expect.poll(async () => (await readStubCalls(page)).queryLogs.length).toBeGreaterThan(0);

    // 首屏请求的参数形状：渲染进程提议的 requestId + from/to + 默认筛选。
    const first = await lastQueryCall(page);
    expect(first.requestId, '渲染进程应提议 requestId 供取消通道精确命中').toMatch(/^q-[a-z0-9-]+$/);
    expect(typeof first.from).toBe('number');
    expect(typeof first.to).toBe('number');
    expect(Number(first.to)).toBeGreaterThan(Number(first.from));
    expect(first.page).toBe(1);
    // meta 与首屏查询并行（LogsView onMounted: void loadMeta(); await refresh()），
    // 首屏 pageSize 是默认 200；meta 落地后（含手动刷新）才用 meta.pageSize。
    expect(first.pageSize, '首屏查询在 meta 返回前发出，用默认 200').toBe(200);
    expect(first.levels).toEqual([]);
    expect(first.sources).toEqual([]);

    // meta 行出现 = meta.value 已写入（版本/平台/每页条数由它渲染）。
    await expect(page.getByText(/版本 9\.9\.9-stub/)).toBeVisible();
    // 手动刷新一次让后续查询走 meta.pageSize=5（12 条 → 3 页，翻页才有意义）。
    await page.getByRole('button', { name: /手动刷新/ }).click();
    await expect.poll(async () => (await lastQueryCall(page)).pageSize).toBe(5);

    // 列表与统计：12 条、每页 5 条 → 3 页。
    await expect(page.getByText(/共 12 条 · 第 1 页/)).toBeVisible();
    await expect(page.getByText('task.event.0', { exact: true })).toBeVisible();

    // 翻页：下一页 → page:2；再下一页 → page:3（hasMore=false → 按钮禁用）。
    await page.getByRole('button', { name: '下一页' }).click();
    await expect.poll(async () => (await lastQueryCall(page)).page).toBe(2);
    await expect(page.getByText('task.event.5', { exact: true })).toBeVisible();
    await expect(page.getByText(/第 2 页/)).toBeVisible();

    await page.getByRole('button', { name: '下一页' }).click();
    await expect.poll(async () => (await lastQueryCall(page)).page).toBe(3);
    await expect(page.getByText('task.event.10', { exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: '下一页' }), '最后一页不得再提供下一页').toBeDisabled();

    await page.getByRole('button', { name: '上一页' }).click();
    await page.getByRole('button', { name: '上一页' }).click();
    await expect.poll(async () => (await lastQueryCall(page)).page).toBe(1);

    // 关键词筛选：300 ms 防抖后进入生效筛选并回到第 1 页。
    // 'event.1' 命中 event.1 / event.10 / event.11 → 3 条。
    await page.getByPlaceholder('事件名或错误摘要').fill('event.1');
    await expect.poll(async () => (await lastQueryCall(page)).keyword, '关键词防抖后应进入查询参数').toBe('event.1');
    await expect(page.getByText(/共 3 条/)).toBeVisible();

    // 级别筛选与关键词取交集：ERROR 只在 i=0/4/8，与 'event.1' 无交集 → 合法空态。
    await page.locator('input[type="checkbox"][value="ERROR"]').check();
    await expect
      .poll(async () => (await lastQueryCall(page)).levels)
      .toEqual(['ERROR']);
    await expect(page.getByText('当前筛选条件下没有日志记录'), '合法空筛选应显示空态（与错误态区分）').toBeVisible();

    // 清空关键词 → 仅 ERROR 筛选 → 3 条（i=0/4/8）。
    await page.getByPlaceholder('事件名或错误摘要').fill('');
    await expect.poll(async () => (await lastQueryCall(page)).keyword).toBeUndefined();
    await expect(page.getByText(/共 3 条/)).toBeVisible();
    await page.locator('input[type="checkbox"][value="ERROR"]').uncheck();
    await expect.poll(async () => (await lastQueryCall(page)).levels).toEqual([]);
    await expect(page.getByText(/共 12 条/)).toBeVisible();

    // 会话筛选：「本次启动」把 meta.sessionId 编码进查询参数（option value = sessionId）。
    await page.getByLabel('会话').selectOption(SESSION_ID);
    await expect.poll(async () => (await lastQueryCall(page)).sessionId).toBe(SESSION_ID);

    // 时间范围：选「最近 1 小时」→ from = to - 3600000（桩只透传断言参数）。
    await page.getByLabel('时间范围').selectOption({ value: '1h' });
    await expect
      .poll(async () => {
        const call = await lastQueryCall(page);
        return Number(call.to) - Number(call.from);
      })
      .toBe(3_600_000);

    // 手动刷新：新增一次查询，总数不变。
    const before = (await readStubCalls(page)).queryLogs.length;
    await page.getByRole('button', { name: /手动刷新/ }).click();
    await expect.poll(async () => (await readStubCalls(page)).queryLogs.length).toBe(before + 1);
    await expect(page.getByText(/共 12 条/)).toBeVisible();
  });

  test('「打开日志目录」与「导出诊断包」调用受限通道并如实反馈取消', async ({ page }) => {
    await installDesktopStub(page, { entries: makeEntries(3), pageSize: 5, failure: null });
    await mockShell(page);
    await page.goto('/logs');
    await expect.poll(async () => (await readStubCalls(page)).queryLogs.length).toBeGreaterThan(0);

    await page.getByRole('button', { name: /打开日志目录/ }).click();
    expect((await readStubCalls(page)).openLogDirectory, '点击必须调用 desktopAPI.openLogDirectory').toBe(1);

    await page.getByRole('button', { name: /导出诊断包/ }).click();
    await expect.poll(async () => (await readStubCalls(page)).exportDiagnostics.length).toBe(1);
    const arg = (await readStubCalls(page)).exportDiagnostics[0];
    expect(typeof arg.from, '导出请求必须携带 from/to 时间窗').toBe('number');
    expect(Number(arg.to)).toBeGreaterThan(Number(arg.from));
    // 桩返回 canceled → 页面必须如实提示取消，而不是伪造成功。
    await expect(page.getByText('已取消导出，未生成任何文件。')).toBeVisible();
  });

  test('queryLogs 返回错误结构时显示错误而非零条记录；恢复后可手动刷新', async ({ page }) => {
    await installDesktopStub(page, {
      entries: makeEntries(3),
      pageSize: 5,
      failure: { code: 'LOG_QUERY_TIMEOUT', message: '日志查询超过 10 秒未完成，已中止，可重试', retryable: true },
    });
    await mockShell(page);
    await page.goto('/logs');

    // 失败结果仍是完整结构：页面必须显示错误提示，而不是伪装成空列表。
    await expect(page.getByText(/查询超时，可重试/)).toBeVisible();
    await expect(page.getByText('当前筛选条件下没有日志记录'), '错误时不得显示成"没有日志记录"').toHaveCount(0);
    await expect(page.getByText('正在读取日志…'), '查询结束后不得残留加载提示').toHaveCount(0);

    // 桩恢复成功后，手动刷新必须让页面回到正常列表（错误不卡死）。
    await page.evaluate(() => {
      (window as Window & { __desktopStub?: { failure: StubFailure | null } }).__desktopStub!.failure = null;
    });
    await page.getByRole('button', { name: /手动刷新/ }).click();
    await expect(page.getByText('task.event.0', { exact: true })).toBeVisible();
    await expect(page.getByText(/查询超时/)).toHaveCount(0);
    await expect(page.getByText(/共 3 条/)).toBeVisible();
  });

  for (const code of ['EACCES', 'EPERM', 'EIO']) {
    test(`读取异常 ${code} 显示红色错误态而非零记录，恢复后清除`, async ({ page }) => {
      await installDesktopStub(page, {
        entries: makeEntries(3), pageSize: 5,
        failure: { code: 'LOG_QUERY_FAILED', message: `桌面日志读取失败（${code}）` },
      });
      await mockShell(page);
      await page.goto('/logs');
      const alert = page.getByRole('alert');
      await expect(alert).toContainText(code);
      await expect(alert).toHaveClass(/bg-err\/10/);
      await expect(alert).toContainText('手动刷新');
      await expect(page.getByText(/查询失败 · 未取得日志结果/)).toBeVisible();
      await expect(page.getByText(/共 0 条|当前筛选条件下没有日志记录|缺失文件/)).toHaveCount(0);
      await page.evaluate(() => {
        (window as Window & { __desktopStub: { failure: StubFailure | null } }).__desktopStub.failure = null;
      });
      await page.getByRole('button', { name: /手动刷新/ }).click();
      await expect(alert).toHaveCount(0);
      await expect(page.getByText(/共 3 条/)).toBeVisible();
    });
  }

  test('已有记录后读取失败保留旧数据并标注，导出不能清除查询错误', async ({ page }) => {
    await installDesktopStub(page, { entries: makeEntries(3), pageSize: 5, failure: null });
    await mockShell(page);
    await page.goto('/logs');
    await expect(page.getByText(/共 3 条/)).toBeVisible();
    await page.evaluate(() => {
      (window as Window & { __desktopStub: { failure: StubFailure | null } }).__desktopStub.failure = {
        code: 'LOG_QUERY_FAILED', message: '后台日志读取失败：权限拒绝（EACCES）',
      };
    });
    await page.getByRole('button', { name: /手动刷新/ }).click();
    await expect(page.getByRole('alert')).toContainText('EACCES');
    await expect(page.getByText(/保留上次成功读取的记录/)).toBeVisible();
    await expect(page.getByText('task.event.0', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: /导出诊断包/ }).click();
    await expect(page.getByRole('alert')).toContainText('EACCES');
  });

  test('尚未生成日志文件属于正常空态，不显示读取失败', async ({ page }) => {
    await installDesktopStub(page, {
      entries: [], pageSize: 5, failure: null, unavailable: ['frontend'],
    });
    await mockShell(page);
    await page.goto('/logs');
    await expect(page.getByText(/未找到日志文件（可能尚未生成）：前端/)).toBeVisible();
    await expect(page.getByText('当前筛选条件下没有日志记录。')).toBeVisible();
    await expect(page.getByRole('alert')).toHaveCount(0);
  });

  test('自动刷新开关：开启后按周期查询，关闭后不再增发', async ({ page }) => {
    await installDesktopStub(page, { entries: makeEntries(3), pageSize: 5, failure: null });
    await mockShell(page);
    await page.goto('/logs');
    await expect.poll(async () => (await readStubCalls(page)).queryLogs.length).toBe(1);

    // 开启「自动刷新（3秒）」：上一次查询结束后 3 秒发下一轮。
    await page.locator('input[type="checkbox"]').first().check();
    await expect
      .poll(async () => (await readStubCalls(page)).queryLogs.length, { timeout: 10_000 })
      .toBeGreaterThan(1);

    // 关闭后不再增发：等最后一轮在途查询收尾后计数应冻结。
    await page.locator('input[type="checkbox"]').first().uncheck();
    await page.waitForTimeout(500); // 让在途查询与收尾逻辑落定
    const frozen = (await readStubCalls(page)).queryLogs.length;
    await page.waitForTimeout(4_000); // 覆盖一个完整刷新周期
    expect(
      (await readStubCalls(page)).queryLogs.length,
      '关闭自动刷新后不得再增发查询',
    ).toBe(frozen);
  });
});

// ================================================================ 可验证项：隐私遮罩（DOM 级）

test.describe('data-feedback-capture-mask（DOM 级存在性）', () => {
  test('导航账号区与日志详情子树都标注遮罩属性', async ({ page }) => {
    await installDesktopStub(page, { entries: makeEntries(3), pageSize: 5, failure: null });
    await mockShell(page);
    await page.goto('/');
    await expect(page).toHaveURL(/\/today$/);

    // 账号区（用户名 + 退出按钮）整块标在遮罩内；截图像素级遮挡由 Worker A/D 的测试覆盖。
    const mask = page.locator('header [data-feedback-capture-mask]');
    await expect(mask, '导航账号区必须存在 data-feedback-capture-mask').toHaveCount(1);
    await expect(mask).toContainText('alice');
    await expect(mask.getByRole('button', { name: '退出' })).toBeVisible();

    // 日志详情子树按 T4 要求默认纳入遮罩（LogsView 展开详情的 <pre>）。
    await page.locator('nav[aria-label="主导航"]').getByRole('link', { name: '日志', exact: true }).click();
    await expect(page).toHaveURL(/\/logs$/);
    await expect(page.getByText('task.event.0', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: '查看详情' }).first().click();
    const detail = page.locator('pre[data-feedback-capture-mask]');
    await expect(detail, '展开的日志详情子树必须带遮罩标记').toBeVisible();
    await expect(detail).toContainText('task.event.0');
  });
});

// ================================================================ 需 Windows 候选包 / 测试服务（本阶段不可验证）

test.describe('Windows 候选安装包验收（本机不可验证，禁止假通过）', () => {
  test.fixme('干净安装：NSIS 安装 → 首次启动 → 日志页读取真实 jsonl', async () => {
    // 需要：windows-2022 CI 产物 studytool-windows-<ver>（或用户 Windows 机构建）。
    // 验证点：安装器跑完 → 启动 → 顶部「日志」入口 → 列表来自真实
    // %APPDATA%/logs/*.jsonl → 打开日志目录弹出真实目录 → 导出诊断包产出 zip。
  });

  test.fixme('1.0.5 → 候选版升级安装：数据保留、日志页与 IPC 正常', async () => {
    // 需要：已装 1.0.5 的 Windows 环境 + 候选包。
    // 验证点：覆盖安装后数据目录保留；SESSION_ID 换新；日志查询/导出/打开目录正常；
    // electron-updater 的 latest*.yml/blockmap 完整（构建脚本已断言）。
  });

  test.fixme('四档缩放下截图与隐私遮罩像素级验证', async () => {
    // 需要：Windows 机器 + 候选包（浏览器 viewport 模拟无法还原 OS DPI × Electron 截图）。
    // 验证点：100%/125%/150%/175% 四档缩放下反馈截图位图正确、
    // 账号区与日志详情遮罩区域为纯色遮挡而非漏像素。像素比对归 Worker A 修复包的
    // masking 回归测试 + 本项的人工/截图证据，本 spec 不做 DOM 替代断言。
  });

  test.fixme('日志附件：getFeedbackLogs → logProvider 真实字节进入反馈提交', async () => {
    // 需要：Worker C（feedback-logs.ts + 主进程 desktop:getFeedbackLogs handler）与
    // Worker D（logProvider 适配）落地后的候选包 + 授权测试服务。
    // 验证点：面板打开自动采集最近 30 分钟/本会话三个来源 → multipart 附件字节与
    // 冻结契约一致；无记录来源不产空文件；BUSY/TIMEOUT/UNAVAILABLE 中文提示。
    // 浏览器侧行为已由 Worker D 的 e2e/specs/feedback-logs.spec.ts 覆盖，本项只挂 Windows 包验收。
  });

  test.fixme('授权测试服务端到端提交（绝不使用生产服务）', async () => {
    // 需要：可写授权测试反馈服务地址（构建期 VITE_FEEDBACK_API_BASE 指向它，
    // 或测试服务与发行服务同构的专用环境）。发行常量门禁禁止改生产地址，
    // 本项只能在专用测试构建/测试服务上验，候选发行包本身不验提交。
  });
});
