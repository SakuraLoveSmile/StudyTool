import { expect, test, type Page, type Route } from '@playwright/test';
import { HOST_USER_A, mockHostApi } from '../helpers/host-page';

/**
 * STU-3「资料与真题」页回归 spec。
 *
 * 验证边界：
 *  - 资料列表把后端 status 如实翻译成中文（pending→等待解析、processing→解析中、
 *    completed→解析完成、failed→解析失败），failed 展示真实 error_msg 原文，
 *    不把供应商 done 当完成。
 *  - 「查看切片与原文」弹窗：切换文档立即清空旧数据、显式 loading/error、
 *    迟到响应按文档 ID 丢弃、请求失败显示「切片加载失败」而非空内容。
 *  - 宿主 /api/* 全部 route 拦截，不触真实 backend/OCR/反馈服务。
 */

interface MockDoc {
  id: string;
  filename: string;
  file_hash: string;
  file_type: string;
  year: number | null;
  session_name: string | null;
  status: 'pending' | 'processing' | 'completed' | 'failed';
  error_msg: string | null;
  page_count: number;
  created_at: string;
  task_id: string;
}

const DOCS: MockDoc[] = [
  {
    id: 'doc-completed', filename: '组织行为学.pdf', file_hash: 'hash-c1',
    file_type: 'textbook', year: null, session_name: null,
    status: 'completed', error_msg: null, page_count: 12,
    created_at: '2026-09-19T03:00:00', task_id: 'task-c1',
  },
  {
    id: 'doc-pending', filename: '1总论.pdf', file_hash: 'hash-p1',
    file_type: 'supplement', year: null, session_name: null,
    status: 'pending', error_msg: null, page_count: 0,
    created_at: '2026-09-19T04:01:00', task_id: 'task-p1',
  },
  {
    id: 'doc-processing', filename: '2023真题A卷.pdf', file_hash: 'hash-r1',
    file_type: 'exam', year: 2023, session_name: 'A卷',
    status: 'processing', error_msg: null, page_count: 3,
    created_at: '2026-09-19T04:02:00', task_id: 'task-r1',
  },
  {
    id: 'doc-failed', filename: '补充讲义.pdf', file_hash: 'hash-f1',
    file_type: 'supplement', year: null, session_name: null,
    status: 'failed',
    error_msg: '向量化失败：切片批量向量化失败: RuntimeError - 远程向量服务请求失败：ReadTimeout',
    page_count: 5, created_at: '2026-09-19T04:03:00', task_id: 'task-f1',
  },
];

const CHUNKS_COMPLETED = [
  {
    id: 'chk-1', doc_id: 'doc-completed', chunk_index: 0, page_number: 1,
    heading: '第 1 页', paragraph_ref: null,
    content: '第一章 管理的概念与基本职能内容。',
    original_content: '第一章 管理的概念与基本职能内容。',
    parse_error: null, embedding_status: 'completed',
    created_at: '2026-09-19T03:01:00',
  },
  {
    id: 'chk-2', doc_id: 'doc-completed', chunk_index: 1, page_number: 3,
    heading: '第 3 页', paragraph_ref: null,
    content: '第三章 组织结构设计原则。',
    original_content: '第三章 组织结构设计原则。',
    parse_error: null, embedding_status: 'completed',
    created_at: '2026-09-19T03:01:01',
  },
];

const json = (route: Route, status: number, body: unknown) =>
  route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

/** 资料页所需的最小宿主桩：材料/题目/答案/考点/任务历史全部 mock。 */
async function mockMaterialsPage(page: Page) {
  const state = { user: HOST_USER_A as typeof HOST_USER_A | null, csrfToken: null as string | null };
  await mockHostApi(page, state);
  await page.route(/\/api\/materials\/tasks\/history(\?|$)/, (route) =>
    json(route, 200, { tasks: [], total: 0, page: 1, limit: 50, active_count: 0, actionable_count: 0, history_count: 0 }),
  );
  await page.route(/\/api\/materials\/tasks\/active\/list(\?|$)/, (route) => json(route, 200, []));
  await page.route(/\/api\/materials(\?|$)/, (route) => json(route, 200, DOCS));
  await page.route(/\/api\/questions\/answer-candidates(\?|$)/, (route) => json(route, 200, []));
  await page.route(/\/api\/questions(\?|$)/, (route) => json(route, 200, []));
  await page.route(/\/api\/analysis\/knowledge-points(\?|$)/, (route) => json(route, 200, []));
  return state;
}

function docRow(page: Page, filename: string) {
  return page.locator('div.surface', { hasText: filename }).first();
}

test.describe('资料列表状态如实回流', () => {
  test('四种状态各自翻译为中文，failed 展示真实 error_msg', async ({ page }) => {
    await mockMaterialsPage(page);
    await page.goto('/materials');

    await expect(page.getByRole('heading', { name: '资料与真题' })).toBeVisible();

    await expect(docRow(page, '组织行为学.pdf').getByText('解析完成')).toBeVisible();
    await expect(docRow(page, '1总论.pdf').getByText('等待解析')).toBeVisible();
    await expect(docRow(page, '2023真题A卷.pdf').getByText('解析中', { exact: true })).toBeVisible();
    await expect(docRow(page, '补充讲义.pdf').getByText('解析失败')).toBeVisible();

    // failed 文档必须展示后端组合 error_msg 原文，而不是空泛提示。
    const failedRow = docRow(page, '补充讲义.pdf');
    await expect(failedRow.getByText(/向量化失败：切片批量向量化失败/)).toBeVisible();
    await expect(failedRow.getByText(/ReadTimeout/)).toBeVisible();

    // 不得出现裸英文状态泄漏到界面。
    const listArea = page.locator('div.surface', { hasText: '1总论.pdf' });
    await expect(listArea.getByText(/\bpending\b/)).toHaveCount(0);
  });
});

test.describe('「查看切片与原文」弹窗', () => {
  test('正常加载显示切片与页码；内容区显示原文', async ({ page }) => {
    await mockMaterialsPage(page);
    // 注意 Playwright 路由后注册者优先：先登记兜底，再登记文档专属。
    await page.route(/\/api\/materials\/[\w-]+\/chunks(\?|$)/, (route) => json(route, 200, []));
    await page.route(/\/api\/materials\/doc-completed\/chunks(\?|$)/, (route) =>
      json(route, 200, CHUNKS_COMPLETED),
    );

    await page.goto('/materials');
    await docRow(page, '组织行为学.pdf').getByRole('button', { name: '查看切片与原文' }).click();

    const dialog = page.getByRole('dialog');
    await expect(dialog.getByText('第 1 页', { exact: true }).first()).toBeVisible();
    await expect(dialog.getByText('第 3 页', { exact: true }).first()).toBeVisible();
    await expect(dialog.getByText('第一章 管理的概念与基本职能内容。')).toBeVisible();
    await expect(dialog.getByText('第三章 组织结构设计原则。')).toBeVisible();
  });

  test('切片请求失败显示「切片加载失败」而非空内容提示', async ({ page }) => {
    await mockMaterialsPage(page);
    await page.route(/\/api\/materials\/[\w-]+\/chunks(\?|$)/, (route) => json(route, 200, []));
    await page.route(/\/api\/materials\/doc-failed\/chunks(\?|$)/, (route) =>
      json(route, 500, { detail: 'database locked' }),
    );

    await page.goto('/materials');
    await docRow(page, '补充讲义.pdf').getByRole('button', { name: '查看切片与原文' }).click();

    const dialog = page.getByRole('dialog');
    await expect(dialog.getByText('切片加载失败，请稍后重试')).toBeVisible();
    await expect(
      dialog.getByText('该文档尚未生成切片或正在解析中。'),
      '请求失败不得伪装成"没有切片"',
    ).toHaveCount(0);
  });

  test('切换文档立即清空旧切片；迟到响应按文档 ID 丢弃', async ({ page }) => {
    await mockMaterialsPage(page);

    // doc-completed 的切片响应人为延迟：模拟慢请求。
    let releaseSlow: (() => void) | null = null;
    const slowGate = new Promise<void>((resolve) => { releaseSlow = resolve; });
    await page.route(/\/api\/materials\/[\w-]+\/chunks(\?|$)/, (route) => json(route, 200, []));
    await page.route(/\/api\/materials\/doc-completed\/chunks(\?|$)/, async (route) => {
      await slowGate;
      await json(route, 200, CHUNKS_COMPLETED);
    });

    await page.goto('/materials');

    // 打开 doc-completed 的切片弹窗：处于加载中，随后关闭弹窗（请求仍在途）。
    await docRow(page, '组织行为学.pdf').getByRole('button', { name: '查看切片与原文' }).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog.getByText('正在加载切片...')).toBeVisible();
    await dialog.getByRole('button', { name: '关闭' }).click();
    await expect(dialog).toHaveCount(0);

    // 改开 doc-pending（立即返回空数组）→ 显示合法空态。
    await docRow(page, '1总论.pdf').getByRole('button', { name: '查看切片与原文' }).click();
    await expect(dialog.getByText('该文档尚未生成切片或正在解析中。')).toBeVisible();

    // 此时放行 doc-completed 的迟到响应：不得把旧文档切片写进当前弹窗。
    releaseSlow!();
    await page.waitForTimeout(300);
    await expect(
      dialog.getByText('第一章 管理的概念与基本职能内容。'),
      '迟到响应不得覆盖当前文档的弹窗内容',
    ).toHaveCount(0);
    await expect(dialog.getByText('该文档尚未生成切片或正在解析中。')).toBeVisible();
  });

  test('重新打开另一文档时旧切片不残留（先清空再加载）', async ({ page }) => {
    await mockMaterialsPage(page);

    let releaseSlow: (() => void) | null = null;
    const slowGate = new Promise<void>((resolve) => { releaseSlow = resolve; });
    await page.route(/\/api\/materials\/[\w-]+\/chunks(\?|$)/, (route) => json(route, 200, []));
    await page.route(/\/api\/materials\/doc-completed\/chunks(\?|$)/, (route) =>
      json(route, 200, CHUNKS_COMPLETED),
    );
    await page.route(/\/api\/materials\/doc-pending\/chunks(\?|$)/, async (route) => {
      await slowGate;
      await json(route, 200, []);
    });

    await page.goto('/materials');

    // 先看 doc-completed 的正常切片，再改开响应较慢的 doc-pending。
    await docRow(page, '组织行为学.pdf').getByRole('button', { name: '查看切片与原文' }).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog.getByText('第一章 管理的概念与基本职能内容。')).toBeVisible();
    await dialog.getByRole('button', { name: '关闭' }).click();
    await expect(dialog).toHaveCount(0);

    await docRow(page, '1总论.pdf').getByRole('button', { name: '查看切片与原文' }).click();
    await expect(dialog.getByText('正在加载切片...')).toBeVisible();
    await expect(
      dialog.getByText('第一章 管理的概念与基本职能内容。'),
      '加载期间不得残留上一文档的切片内容',
    ).toHaveCount(0);

    releaseSlow!();
    await expect(dialog.getByText('该文档尚未生成切片或正在解析中。')).toBeVisible();
  });

  test('同一文档关闭重开：旧成功响应不得覆盖新内容', async ({ page }) => {
    await mockMaterialsPage(page);

    // 重开后返回的另一组切片：文案与 CHUNKS_COMPLETED 完全区分开。
    const CHUNKS_REOPENED = [
      {
        id: 'chk-9', doc_id: 'doc-completed', chunk_index: 0, page_number: 7,
        heading: '第 7 页', paragraph_ref: null,
        content: '第七章 领导行为理论（重开后的新切片）。',
        original_content: '第七章 领导行为理论（重开后的新切片）。',
        parse_error: null, embedding_status: 'completed',
        created_at: '2026-09-19T03:02:00',
      },
    ];

    // 同一 doc.id 走同一 URL，只能靠请求计数区分第几次：第 1 次挂起在 gate 上，
    // 第 2 次立即返回新切片。
    let requestCount = 0;
    let releaseFirst: (() => void) | null = null;
    const firstGate = new Promise<void>((resolve) => { releaseFirst = resolve; });
    await page.route(/\/api\/materials\/[\w-]+\/chunks(\?|$)/, (route) => json(route, 200, []));
    await page.route(/\/api\/materials\/doc-completed\/chunks(\?|$)/, async (route) => {
      requestCount += 1;
      if (requestCount === 1) {
        await firstGate;
        await json(route, 200, CHUNKS_COMPLETED);
        return;
      }
      await json(route, 200, CHUNKS_REOPENED);
    });

    await page.goto('/materials');

    // 打开弹窗（第 1 次请求挂起）→ 确认 loading → 关闭 → 重开同一文档。
    await docRow(page, '组织行为学.pdf').getByRole('button', { name: '查看切片与原文' }).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog.getByText('正在加载切片...')).toBeVisible();
    await dialog.getByRole('button', { name: '关闭' }).click();
    await expect(dialog).toHaveCount(0);

    await docRow(page, '组织行为学.pdf').getByRole('button', { name: '查看切片与原文' }).click();
    await expect(dialog.getByText('第七章 领导行为理论（重开后的新切片）。')).toBeVisible();

    // 放行第 1 次旧响应：旧内容不得出现，新内容保持展示。
    releaseFirst!();
    await page.waitForTimeout(300);
    await expect(
      dialog.getByText('第一章 管理的概念与基本职能内容。'),
      '旧成功响应不得覆盖同一文档重开后的新内容',
    ).toHaveCount(0);
    await expect(dialog.getByText('第七章 领导行为理论（重开后的新切片）。')).toBeVisible();
  });

  test('同一文档关闭重开：旧失败响应不得写入错误或终结新请求 loading', async ({ page }) => {
    await mockMaterialsPage(page);

    // 第 1 次请求挂起后返回 500；第 2 次请求也挂起在 gate 上（保持 loading）。
    let requestCount = 0;
    let releaseFirst: (() => void) | null = null;
    let releaseSecond: (() => void) | null = null;
    const firstGate = new Promise<void>((resolve) => { releaseFirst = resolve; });
    const secondGate = new Promise<void>((resolve) => { releaseSecond = resolve; });
    await page.route(/\/api\/materials\/[\w-]+\/chunks(\?|$)/, (route) => json(route, 200, []));
    await page.route(/\/api\/materials\/doc-completed\/chunks(\?|$)/, async (route) => {
      requestCount += 1;
      if (requestCount === 1) {
        await firstGate;
        await json(route, 500, { detail: 'stale failure' });
        return;
      }
      await secondGate;
      await json(route, 200, CHUNKS_COMPLETED);
    });

    await page.goto('/materials');

    // 打开弹窗（第 1 次请求挂起）→ 关闭 → 重开同一文档（第 2 次请求挂起，loading 继续）。
    await docRow(page, '组织行为学.pdf').getByRole('button', { name: '查看切片与原文' }).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog.getByText('正在加载切片...')).toBeVisible();
    await dialog.getByRole('button', { name: '关闭' }).click();
    await expect(dialog).toHaveCount(0);

    await docRow(page, '组织行为学.pdf').getByRole('button', { name: '查看切片与原文' }).click();
    await expect(dialog.getByText('正在加载切片...')).toBeVisible();

    // 放行第 1 次旧失败响应：不得写错误提示，也不得终结新请求的 loading。
    releaseFirst!();
    await page.waitForTimeout(300);
    await expect(
      dialog.getByText('切片加载失败，请稍后重试'),
      '旧失败响应不得把错误写进重开后的弹窗',
    ).toHaveCount(0);
    await expect(
      dialog.getByText('正在加载切片...'),
      '旧失败响应不得终结新请求的 loading',
    ).toBeVisible();

    // 放行第 2 次新请求：正常显示切片内容，loading 结束。
    releaseSecond!();
    await expect(dialog.getByText('第一章 管理的概念与基本职能内容。')).toBeVisible();
    await expect(dialog.getByText('正在加载切片...')).toHaveCount(0);
  });
});
