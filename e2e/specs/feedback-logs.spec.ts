import { expect, test, type Page } from '@playwright/test';
import {
  HOST_USER_A, HOST_USER_B, clickPrimarySubmit, closePanel, draftInput, loginInsideWidgetOnly,
  mockFeedbackService, mockHostApi, openPanel, panelText, readWidgetState, typeDraft,
  waitForAutoScreenshot, waitForWidget, widget,
} from '../helpers/host-page';
import { deferred, feedbackRules, type MockService, type RouteRule } from '../helpers/feedback-mock';

/**
 * 桌面受限日志采集 → 反馈组件 logProvider 的接入测试（已配置形态，5275）。
 *
 * `window.desktopAPI` 在真实桌面端由 preload 注入；e2e 用 addInitScript + exposeFunction
 * 装等形桩：`isDesktop: true` + `getFeedbackLogs()`（其余受限方法一律不注入——
 * 宿主其它桌面能力各自按方法门禁，不受影响）。
 *
 * 断言落在真实附件字节（draft.logs 的 blob.text() / 提交 multipart 体）与真实中文错误
 * 文案上，不只断言 provider 被调用。
 */

type LogsImpl = () => Promise<unknown>;

interface DesktopLogsStub {
  /** getFeedbackLogs 被组件调用的次数（跨整页重载累计）。 */
  count(): number;
  /** 换实现：同一页面内下一次调用生效（桩函数本体不重建）。 */
  setImpl(next: LogsImpl): void;
}

/** 安装带 getFeedbackLogs 的 desktopAPI 桩；impl 在 Node 侧执行，可返回受控 Promise。 */
async function installDesktopLogsStub(page: Page, impl: LogsImpl): Promise<DesktopLogsStub> {
  const state = { impl, calls: 0 };
  await page.exposeFunction('__desktopLogsInvoke', () => {
    state.calls += 1;
    return state.impl();
  });
  await page.addInitScript(() => {
    const w = window as unknown as Record<string, unknown>;
    w.desktopAPI = {
      isDesktop: true,
      getFeedbackLogs: () => (w.__desktopLogsInvoke as () => Promise<unknown>)(),
    };
  });
  return {
    count: () => state.calls,
    setImpl: (next) => { state.impl = next; },
  };
}

/** 安装「无有效方法」形态的 desktopAPI（absent 时什么都不装）。 */
async function installDesktopApiShape(page: Page, shape: 'absent' | 'no-method' | 'non-function'): Promise<void> {
  if (shape === 'absent') return;
  await page.addInitScript((kind) => {
    const w = window as unknown as Record<string, unknown>;
    w.desktopAPI = kind === 'no-method'
      ? { isDesktop: true }
      : { isDesktop: true, getFeedbackLogs: 42 };
  }, shape);
}

interface OpenedApp {
  state: { user: typeof HOST_USER_A | null; csrfToken: string | null };
  host: Record<string, number>;
  svc: MockService;
}

async function openConfiguredApp(page: Page, rules: RouteRule[] = []): Promise<OpenedApp> {
  const state = { user: HOST_USER_A as typeof HOST_USER_A | null, csrfToken: null as string | null };
  const host = await mockHostApi(page, state);
  const svc = await mockFeedbackService(page, rules);
  await page.goto('/');
  await waitForWidget(page);
  return { state, host, svc };
}

// ---------------------------------------------------------------- 桩数据

const DESKTOP_NAME = 'studytool-desktop.jsonl';
const BACKEND_NAME = 'studytool-backend.jsonl';
const FRONTEND_NAME = 'studytool-frontend.jsonl';

function okLogs(files: Array<{ filename: string; text: string }>, extra: Record<string, unknown> = {}) {
  return { ok: true, files, truncated: false, unavailable: [], skippedLines: 0, ...extra };
}

function threeFiles(marker: string) {
  return okLogs([
    { filename: DESKTOP_NAME, text: `{"src":"desktop","marker":"${marker}"}\n{"src":"desktop","event":"第二行"}` },
    { filename: BACKEND_NAME, text: `{"src":"backend","marker":"${marker}"}` },
    { filename: FRONTEND_NAME, text: `{"src":"frontend","marker":"${marker}"}` },
  ]);
}

function textsOf(marker: string): string[] {
  return [
    `{"src":"desktop","marker":"${marker}"}\n{"src":"desktop","event":"第二行"}`,
    `{"src":"backend","marker":"${marker}"}`,
    `{"src":"frontend","marker":"${marker}"}`,
  ];
}

// ---------------------------------------------------------------- 组件侧定位

const logItems = (page: Page) => page.locator('feedback-widget').locator('.fb-log-item');
const logNames = (page: Page) => page.locator('feedback-widget').locator('.fb-log-name');
const logBadges = (page: Page) => page.locator('feedback-widget').locator('.fb-log-badge');
const logsCount = (page: Page) => page.locator('feedback-widget').locator('.fb-logs-count');
const logError = (page: Page) => page.locator('feedback-widget').locator('.fb-log-error');
const logStatus = (page: Page) => page.locator('feedback-widget').locator('.fb-log-status');
const previewBtn = (page: Page) => page.locator('feedback-widget').locator('button.fb-log-btn-preview');
const removeBtn = (page: Page) => page.locator('feedback-widget').locator('button.fb-log-btn-remove');
const previewBody = (page: Page) => page.locator('feedback-widget').locator('.fb-log-preview-body');

/** 读取组件草稿内日志附件的真实字节（draft.logs[].blob.text()）。 */
async function readDraftLogs(page: Page): Promise<Array<{ filename: string; source: string; size: number; text: string }>> {
  return page.evaluate(async () => {
    const el = document.querySelector('feedback-widget') as unknown as Record<string, unknown> | null;
    const logs = ((el?.draft as { logs?: Array<{ filename: string; source: string; blob: Blob }> } | undefined)?.logs) ?? [];
    return Promise.all(logs.map(async (l) => ({
      filename: l.filename,
      source: l.source,
      size: l.blob.size,
      text: await l.blob.text(),
    })));
  });
}

/** 读取组件公开的 logProvider 属性（未安装时应为 undefined）。 */
async function readLogProvider(page: Page): Promise<unknown> {
  return page.evaluate(() => (document.querySelector('feedback-widget') as unknown as { logProvider?: unknown })?.logProvider);
}

// ---------------------------------------------------------------- multipart 解析

interface LogPart { filename: string; bytes: string }

/** 从 multipart 请求体取出 name="logs" 的各附件段（真实字节）。 */
function extractLogParts(body: string): LogPart[] {
  const parts: LogPart[] = [];
  const re = /name="logs";\s*filename="([^"]+)"[\s\S]*?\r?\n\r?\n([\s\S]*?)\r?\n--/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(body)) !== null) parts.push({ filename: m[1], bytes: m[2] });
  return parts;
}

function extractMetadata(body: string): { logs?: Array<{ filename: string; source: string }> } {
  const m = body.match(/name="metadata"\r?\n\r?\n([\s\S]*?)\r?\n--/);
  return m ? (JSON.parse(m[1]) as { logs?: Array<{ filename: string; source: string }> }) : {};
}

// ---------------------------------------------------------------- 正常路径

test.describe('桌面日志自动采集', () => {
  test('返回三文件：自动附件出现、可预览、可删除，提交 multipart 字节与预览一致', async ({ page }) => {
    const stub = await installDesktopLogsStub(page, async () => threeFiles('GEN-A'));
    const { host, svc } = await openConfiguredApp(page, [
      feedbackRules.loginOk(),
      feedbackRules.sessionOk(),
      feedbackRules.submitOk('fb-logs-1'),
      feedbackRules.recordArchived('fb-logs-1'),
    ]);

    await openPanel(page);
    await expect(logItems(page)).toHaveCount(3);
    await expect(logsCount(page)).toHaveText(' (3/3)');
    expect(stub.count(), '打开面板应恰好自动采集一次').toBe(1);
    expect(await logNames(page).allTextContents()).toEqual([DESKTOP_NAME, BACKEND_NAME, FRONTEND_NAME]);
    expect(await logBadges(page).allTextContents()).toEqual(['自动', '自动', '自动']);
    await expect(logError(page)).toBeHidden();

    // 附件字节：draft.logs 里的 Blob 内容必须与桩返回的 text 完全一致。
    const drafts = await readDraftLogs(page);
    expect(drafts.map((d) => d.filename)).toEqual([DESKTOP_NAME, BACKEND_NAME, FRONTEND_NAME]);
    expect(drafts.map((d) => d.source)).toEqual(['auto', 'auto', 'auto']);
    expect(drafts.map((d) => d.text)).toEqual(textsOf('GEN-A'));

    // 预览：面板内读出的内容与桩 text 一致（截图预览是同一 Blob）。
    await previewBtn(page).first().click();
    await expect(previewBody(page)).toHaveText(textsOf('GEN-A')[0]);
    const previewed = (await previewBody(page).textContent()) ?? '';
    await page.locator('feedback-widget').locator('.fb-log-preview-modal .fb-close').click();

    // 删除第三个附件。
    await removeBtn(page).nth(2).click();
    await expect(logItems(page)).toHaveCount(2);
    await expect(logsCount(page)).toHaveText(' (2/3)');

    // 提交：登录在组件内完成，multipart 里的 logs 段字节必须与预览/桩内容一致。
    await typeDraft(page, '带日志附件的反馈内容');
    await loginInsideWidgetOnly(page, 'tester', 'not-a-real-password');
    await expect.poll(() => svc.countOf('POST', '/api/auth/login')).toBe(1);
    expect(host.login, '组件内登录不应触碰宿主登录接口').toBe(0);

    await clickPrimarySubmit(page);
    await expect.poll(() => svc.countOf('POST', '/api/feedback')).toBe(1);
    const sent = svc.all('POST', '/api/feedback')[0];
    expect(sent.headers['authorization']).toBe('Bearer mock-token-0001');

    const parts = extractLogParts(sent.bodyText);
    expect(parts.map((p) => p.filename), '删除后只应提交剩余两个日志附件').toEqual([DESKTOP_NAME, BACKEND_NAME]);
    expect(parts[0].bytes, 'multipart 日志字节必须与预览内容一致').toBe(previewed);
    expect(parts[0].bytes).toBe(textsOf('GEN-A')[0]);
    expect(parts[1].bytes).toBe(textsOf('GEN-A')[1]);

    const meta = extractMetadata(sent.bodyText);
    expect(meta.logs).toEqual([
      { filename: DESKTOP_NAME, source: 'auto' },
      { filename: BACKEND_NAME, source: 'auto' },
    ]);

    // 提交成功后草稿清空、附件随之清空。
    await expect.poll(async () => (await readWidgetState(page)).lastFeedbackId).toBe('fb-logs-1');
    await expect(logItems(page)).toHaveCount(0);
  });

  test('已采集后重开面板不重采；用户删光自动附件后不会被普通重开补回', async ({ page }) => {
    const stub = await installDesktopLogsStub(page, async () => threeFiles('GEN-A'));
    await openConfiguredApp(page);

    await openPanel(page);
    await expect(logItems(page)).toHaveCount(3);

    // 普通关闭→重开：logsCollected 已置位，不得再次调用 getFeedbackLogs。
    await closePanel(page);
    await openPanel(page);
    await expect(logItems(page)).toHaveCount(3);
    expect(stub.count(), '重开面板不得重采').toBe(1);

    // 用户主动删光自动附件。
    for (let i = 0; i < 3; i++) {
      await removeBtn(page).first().click();
    }
    await expect(logItems(page)).toHaveCount(0);
    await expect(logsCount(page)).toHaveText(' (0/3)');

    // 同一草稿再重开：删除是用户决定，不得被补回。
    await closePanel(page);
    await openPanel(page);
    await expect(logItems(page)).toHaveCount(0);
    await expect(logError(page)).toBeHidden();
    expect(stub.count(), '删除后重开仍不得重采').toBe(1);
  });

  test('逐文件校验：白名单外 / 重复 / 超 256KiB / 超出 3 个的文件被丢弃，其余照常附加', async ({ page }) => {
    const oversize = 'x'.repeat(300 * 1024); // > 宿主 256KiB 上限
    const stub = await installDesktopLogsStub(page, async () => okLogs([
      { filename: 'evil.exe', text: 'not allowed' },                  // 白名单外
      { filename: DESKTOP_NAME, text: 'D1' },
      { filename: DESKTOP_NAME, text: 'D2-duplicate' },               // 重复文件名
      { filename: BACKEND_NAME, text: oversize },                     // 超 256KiB
      { filename: BACKEND_NAME, text: 'B1' },
      { filename: FRONTEND_NAME, text: 'F1' },
      { filename: FRONTEND_NAME, text: 'F2-fourth-valid' },           // 第 4 个有效文件
    ]));
    await openConfiguredApp(page);

    await openPanel(page);
    await expect(logItems(page)).toHaveCount(3);
    expect(await logNames(page).allTextContents()).toEqual([DESKTOP_NAME, BACKEND_NAME, FRONTEND_NAME]);
    const drafts = await readDraftLogs(page);
    expect(drafts.map((d) => d.text)).toEqual(['D1', 'B1', 'F1']);
    await expect(logError(page)).toBeHidden();
    expect(stub.count()).toBe(1);
  });

  test('files 为空数组：无附件、不报错、面板正常', async ({ page }) => {
    const stub = await installDesktopLogsStub(page, async () => okLogs([]));
    await openConfiguredApp(page);

    await openPanel(page);
    await expect(logItems(page)).toHaveCount(0);
    await expect(logsCount(page)).toHaveText(' (0/3)');
    await expect(logError(page)).toBeHidden();
    expect(stub.count()).toBe(1);

    await typeDraft(page, '没有日志也能写反馈');
    await expect(draftInput(page)).toHaveValue('没有日志也能写反馈');
  });
});

// ---------------------------------------------------------------- 无通道形态

test.describe('无日志通道', () => {
  for (const variant of [
    { name: 'desktopAPI 整个不存在', shape: 'absent' },
    { name: 'desktopAPI 无此方法', shape: 'no-method' },
    { name: 'getFeedbackLogs 非函数', shape: 'non-function' },
  ] as const) {
    test(`${variant.name}：不安装 provider、不采集、不报错、面板正常`, async ({ page }) => {
      await installDesktopApiShape(page, variant.shape);
      await openConfiguredApp(page);

      expect(await readLogProvider(page), '无有效通道时不得安装 logProvider').toBeUndefined();
      await openPanel(page);
      await expect(logItems(page)).toHaveCount(0);
      await expect(logError(page)).toBeHidden();
      await expect(logStatus(page)).toBeHidden();

      // 面板其余功能不受影响。
      await typeDraft(page, '没有桌面通道也能写反馈');
      await expect(draftInput(page)).toHaveValue('没有桌面通道也能写反馈');
    });
  }
});

// ---------------------------------------------------------------- 失败路径

test.describe('日志采集失败', () => {
  for (const [code, expected] of [
    ['LOG_FEEDBACK_BUSY', '日志采集进行中，请稍后重试'],
    ['LOG_FEEDBACK_TIMEOUT', '日志采集超时'],
    ['LOG_FEEDBACK_CANCELED', '日志采集已取消'],
    ['LOG_FEEDBACK_UNAVAILABLE', '日志不可用'],
    ['LOG_FEEDBACK_FAILED', '日志采集失败'],
    ['LOG_FEEDBACK_SOMETHING_ELSE', '日志采集失败'],
  ] as const) {
    test(`ok:false ${code} → 显示「${expected}」，不透传后端原文`, async ({ page }) => {
      const stub = await installDesktopLogsStub(page, async () => ({
        ok: false,
        code,
        message: 'internal detail: /var/log/studytool/secret-path.jsonl permission denied',
      }));
      await openConfiguredApp(page);

      await openPanel(page);
      await expect(logError(page)).toHaveText(expected);
      await expect(logError(page)).not.toContainText('/var/log');
      await expect(logError(page)).not.toContainText('secret-path');
      await expect(logItems(page)).toHaveCount(0);
      expect(stub.count()).toBe(1);
    });
  }

  test('IPC 通道 reject → 显示通道异常，不泄露底层错误', async ({ page }) => {
    const stub = await installDesktopLogsStub(page, async () => {
      throw new Error('ENOENT connect /Users/dev/secret/ipc.sock');
    });
    await openConfiguredApp(page);

    await openPanel(page);
    await expect(logError(page)).toHaveText('日志采集通道异常');
    await expect(logError(page)).not.toContainText('ENOENT');
    await expect(logItems(page)).toHaveCount(0);
    expect(stub.count()).toBe(1);
  });

  for (const [name, payload] of [
    ['ok 非布尔', { ok: 'yes', files: [] }],
    ['files 非数组', { ok: true, files: 'nope', truncated: false, unavailable: [], skippedLines: 0 }],
    ['文件项缺 text', { ok: true, files: [{ filename: DESKTOP_NAME }], truncated: false, unavailable: [], skippedLines: 0 }],
    ['unavailable 含非法来源', { ok: true, files: [], truncated: false, unavailable: ['evil-source'], skippedLines: 0 }],
    ['skippedLines 非数字', { ok: true, files: [], truncated: false, unavailable: [], skippedLines: '0' }],
    ['整体非对象', null],
    ['ok:false 缺 message', { ok: false, code: 'LOG_FEEDBACK_FAILED' }],
  ] as const) {
    test(`结构非法（${name}）→ 显示「日志响应结构非法」`, async ({ page }) => {
      await installDesktopLogsStub(page, async () => payload);
      await openConfiguredApp(page);

      await openPanel(page);
      await expect(logError(page)).toHaveText('日志响应结构非法');
      await expect(logItems(page)).toHaveCount(0);
    });
  }
});

// ---------------------------------------------------------------- 生命周期 / 世代

test.describe('日志采集生命周期', () => {
  test('旧实例 provider 晚于新身份返回：结果作废，不写入新旧实例', async ({ page }) => {
    const gate = deferred<unknown>();
    let call = 0;
    const stub = await installDesktopLogsStub(page, () => {
      call += 1;
      return call === 1 ? (gate.promise as Promise<unknown>) : Promise.resolve(threeFiles('GEN-B'));
    });
    await openConfiguredApp(page);

    await openPanel(page);
    await expect(logStatus(page)).toHaveText('正在自动采集日志…');
    expect(stub.count()).toBe(1);

    // 保留旧实例引用，供销毁后断言。
    await page.evaluate(() => {
      (window as unknown as Record<string, unknown>).__oldWidget = document.querySelector('feedback-widget');
    });

    // 应用内登出 → 销毁旧实例；应用内重新登录 → sessionEpoch++ → 全新实例。
    await page.getByRole('button', { name: '退出' }).click();
    await expect(widget(page)).toHaveCount(0);
    await page.locator('#login-username').fill('alice');
    await page.locator('#login-password').fill('not-a-real-password');
    await page.getByRole('button', { name: '登录' }).click();
    await waitForWidget(page);

    // 新实例开面板 → 第二次采集（GEN-B 立即返回）。
    await openPanel(page);
    await expect(logItems(page)).toHaveCount(3);

    // 此刻才放行旧采集：旧 provider 的 isAlive 已失效，结果必须作废。
    gate.resolve(threeFiles('GEN-A'));
    await page.waitForTimeout(400);

    const drafts = await readDraftLogs(page);
    expect(drafts.map((d) => d.text), '新实例只应持有 GEN-B 的日志内容').toEqual(textsOf('GEN-B'));
    const oldLogs = await page.evaluate(() =>
      (((window as unknown as Record<string, { draft?: { logs?: unknown[] } }>).__oldWidget?.draft?.logs) ?? []).length);
    expect(oldLogs, '迟到结果不得写回已销毁的旧实例').toBe(0);
    expect(stub.count(), '旧 provider 作废不得触发额外采集').toBe(2);
  });

  test('A→B 身份切换（整页重载）：新身份采集自己的日志，旧迟到结果不进新实例', async ({ page }) => {
    const gate = deferred<unknown>();
    let call = 0;
    const stub = await installDesktopLogsStub(page, () => {
      call += 1;
      return call === 1 ? (gate.promise as Promise<unknown>) : Promise.resolve(threeFiles('GEN-B'));
    });
    const { state } = await openConfiguredApp(page);

    await openPanel(page);
    await expect(logStatus(page)).toHaveText('正在自动采集日志…');
    expect(stub.count()).toBe(1);

    await page.getByRole('button', { name: '退出' }).click();
    await expect(widget(page)).toHaveCount(0);

    // 旧文档仍存活时放行旧采集：旧实例已销毁，provider 的 isAlive 失效 → 结果作废。
    gate.resolve(threeFiles('GEN-A'));
    await page.waitForTimeout(400);

    state.user = HOST_USER_B;
    state.csrfToken = 'csrf-rag-0002';
    await page.goto('/today');
    await waitForWidget(page);

    // B 身份实例开面板 → 第二次采集，只应得到 GEN-B。
    await openPanel(page);
    await expect(logItems(page)).toHaveCount(3);

    const drafts = await readDraftLogs(page);
    expect(drafts.map((d) => d.text), 'B 身份实例只应持有自己的 GEN-B 日志').toEqual(textsOf('GEN-B'));
    await expect(logError(page)).toBeHidden();
    expect(stub.count()).toBe(2);
  });

  test('提交失败重试：复用冻结附件与同一幂等键，不重新采集', async ({ page }) => {
    const stub = await installDesktopLogsStub(page, async () => threeFiles('GEN-A'));
    const { svc } = await openConfiguredApp(page, [
      feedbackRules.loginOk(),
      feedbackRules.sessionOk(),
      feedbackRules.submitServerError(),
    ]);

    await openPanel(page);
    await expect(logItems(page)).toHaveCount(3);
    await typeDraft(page, '网络故障也要保留日志附件');
    await loginInsideWidgetOnly(page, 'tester', 'not-a-real-password');
    await expect.poll(() => svc.countOf('POST', '/api/auth/login')).toBe(1);

    await clickPrimarySubmit(page);
    await expect.poll(() => svc.countOf('POST', '/api/feedback')).toBe(1);
    await expect.poll(async () => (await panelText(page)).includes('重试提交'), { timeout: 10_000 }).toBe(true);

    const first = svc.all('POST', '/api/feedback')[0];
    expect(extractLogParts(first.bodyText).map((p) => p.filename)).toEqual([DESKTOP_NAME, BACKEND_NAME, FRONTEND_NAME]);

    await page.locator('feedback-widget').locator('button', { hasText: '重试提交' }).first().click();
    await expect.poll(() => svc.countOf('POST', '/api/feedback')).toBe(2);

    const retry = svc.all('POST', '/api/feedback')[1];
    expect(retry.headers['idempotency-key'], '重试必须复用同一幂等键').toBe(first.headers['idempotency-key']);
    expect(extractLogParts(retry.bodyText), '重试必须携带同一批冻结日志字节').toEqual(extractLogParts(first.bodyText));
    expect(stub.count(), '重试不得重新采集日志').toBe(1);
  });
});

// ---------------------------------------------------------------- 回归

test.describe('回归：日志附件不干扰既有行为', () => {
  test('路由与主题切换不重建实例、不动日志附件与草稿', async ({ page }) => {
    const stub = await installDesktopLogsStub(page, async () => threeFiles('GEN-A'));
    await openConfiguredApp(page, [feedbackRules.loginOk(), feedbackRules.sessionOk()]);

    await openPanel(page);
    await expect(logItems(page)).toHaveCount(3);
    await typeDraft(page, '切换路由和主题后仍在');
    const before = await waitForAutoScreenshot(page);
    expect(before.draftBytes).toBeGreaterThan(0);

    await page.evaluate(() => {
      (document.querySelector('feedback-widget') as unknown as Record<string, unknown>).__marker = 'kept';
    });

    // 路由切换：同一实例，附件与草稿原样保留。
    await page.getByRole('link', { name: '资料与真题' }).first().click();
    await expect(page).toHaveURL(/\/materials$/, { timeout: 10_000 });
    await expect(logItems(page)).toHaveCount(3);
    const afterRoute = await readWidgetState(page);
    expect(afterRoute.draftText).toBe('切换路由和主题后仍在');
    expect(afterRoute.draftBytes).toBe(before.draftBytes);
    expect(stub.count(), '路由切换不得重采').toBe(1);

    // 主题切换：先关面板再操作导航栏下拉，草稿留在实例内。
    await closePanel(page);
    await page.getByRole('button', { name: /外观设置/ }).click();
    await page.getByRole('menuitemradio', { name: '深色' }).click();
    await expect.poll(() => page.evaluate(() => document.documentElement.dataset.theme)).toBe('dark');

    await openPanel(page);
    await expect(logItems(page)).toHaveCount(3);
    const afterTheme = await readWidgetState(page);
    expect(afterTheme.draftText).toBe('切换路由和主题后仍在');
    expect(await page.evaluate(() =>
      (document.querySelector('feedback-widget') as unknown as Record<string, unknown>).__marker)).toBe('kept');
    await expect.poll(() => page.evaluate(() => document.querySelector('feedback-widget')?.getAttribute('theme'))).toBe('dark');
    expect(stub.count(), '主题切换不得重采').toBe(1);
  });
});
