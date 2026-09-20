/**
 * 学习助手宿主页面 mock 与反馈组件操作助手。
 *
 * 宿主的 `/api/*` 一律拦截：**不依赖真实 backend**，也不调用真实登录接口。
 * 反馈服务的跨 origin 请求由 feedback-mock.ts 处理。
 *
 * 选择器来自组件的实际 Shadow DOM（0.4.0 源码核对一致，结构见 README）：
 *   启动入口 `button`（文案「反馈」）/ 面板 `#fb-panel` / 草稿框 `#fb-textarea`
 *   截图区按钮「截取当前页面」「重新截图」「移除截图」
 *   底部提交按钮「登录并提交」
 */

import { expect, type Page } from '@playwright/test';
import { installMockService, type MockService, type RouteRule } from './feedback-mock';

export const APP_OFF_URL = 'http://127.0.0.1:5274';
export const APP_ON_URL = 'http://127.0.0.1:5275';
export const FEEDBACK_BASE = 'http://127.0.0.1:5199';
export const FEEDBACK_BASE_B = 'http://127.0.0.1:5198';

export const HOST_USER_A = { id: 'rag-user-a', username: 'alice', must_change_password: false };
export const HOST_USER_B = { id: 'rag-user-b', username: 'bob', must_change_password: false };

export interface HostUser { id: string; username: string; must_change_password: boolean }
export interface HostState { user: HostUser | null; csrfToken: string | null }

/** 宿主 API 的 mock：返回可控的当前用户与 CSRF 令牌，并记录宿主请求次数。 */
export async function mockHostApi(page: Page, state: HostState): Promise<Record<string, number>> {
  const counts: Record<string, number> = { me: 0, login: 0, logout: 0 };
  const json = (route: import('@playwright/test').Route, status: number, body: unknown) =>
    route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

  await page.route(/\/api\/auth\/me(\?|$)/, async (route) => {
    counts.me += 1;
    await json(route, 200, {
      user: state.user,
      csrf_token: state.user ? (state.csrfToken ??= 'csrf-rag-0001') : null,
      auth_required: true,
    });
  });
  await page.route(/\/api\/auth\/login(\?|$)/, async (route) => {
    counts.login += 1;
    state.user = HOST_USER_A;
    state.csrfToken = 'csrf-rag-0001';
    await json(route, 200, { user: state.user, csrf_token: state.csrfToken, auth_required: true });
  });
  await page.route(/\/api\/auth\/logout(\?|$)/, async (route) => {
    counts.logout += 1;
    state.user = null;
    state.csrfToken = null;
    await json(route, 200, {});
  });

  // 学习页 / 抽屉的最小桩数据：让外壳正常渲染，避免走真实 backend。
  await page.route(/\/api\/learning\/settings(\?|$)/, (route) =>
    json(route, 200, { daily_new_limit: 10, daily_review_limit: 20, session_size: 5, auto_advance: true }));
  await page.route(/\/api\/learning\/chapters(\?|$)/, (route) => json(route, 200, []));
  await page.route(/\/api\/learning\/today(\?|$)/, (route) => json(route, 200, { units: [], state_version: 1 }));
  await page.route(/\/api\/materials\/tasks\/active\/list(\?|$)/, (route) => json(route, 200, []));

  return counts;
}

/** 装好反馈服务 mock，返回服务句柄。 */
export async function mockFeedbackService(page: Page, rules: RouteRule[], base = FEEDBACK_BASE): Promise<MockService> {
  return installMockService(page, base, rules);
}

export interface ProbeSnapshot {
  apiBaseSets: Array<{ value: string | null; wasConnected: boolean }>;
  submitted: Array<{ feedbackId: string; status: string; replayed: boolean }>;
}

/** 在任何页面脚本执行前装探针：记录 apiBase 赋值顺序与 feedback-submitted 事件。 */
export async function installProbe(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const w = window as unknown as { __probe: ProbeSnapshot };
    w.__probe = { apiBaseSets: [], submitted: [] };
    document.addEventListener('feedback-submitted', (event: Event) => {
      const detail = (event as CustomEvent).detail as { feedbackId?: string; status?: string; replayed?: boolean } | undefined;
      w.__probe.submitted.push({
        feedbackId: String(detail?.feedbackId ?? ''),
        status: String(detail?.status ?? ''),
        replayed: Boolean(detail?.replayed),
      });
    });
    const install = () => {
      const ctor = customElements.get('feedback-widget');
      if (!ctor) return false;
      const proto = ctor.prototype as unknown as Record<string, unknown>;
      const descriptor = Object.getOwnPropertyDescriptor(proto, 'apiBase');
      if (descriptor?.set) {
        Object.defineProperty(proto, 'apiBase', {
          configurable: true,
          enumerable: descriptor.enumerable,
          get: descriptor.get,
          set(this: HTMLElement, value: string | null) {
            w.__probe.apiBaseSets.push({ value, wasConnected: this.isConnected });
            descriptor.set!.call(this, value);
          },
        });
      }
      return true;
    };
    const timer = setInterval(() => { if (install()) clearInterval(timer); }, 5);
  });
}

export async function readProbe(page: Page): Promise<ProbeSnapshot> {
  return page.evaluate(() => {
    const w = window as unknown as { __probe?: ProbeSnapshot };
    return w.__probe ?? { apiBaseSets: [], submitted: [] };
  });
}

/** 记录 `api-base` 属性变化（属性回调探针，辅助证据：证明清理发生在移除之前）。 */
export async function installAttributeProbe(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const w = window as unknown as { __attr: Array<{ from: string | null; to: string | null; connected: boolean }> };
    w.__attr = [];
    const install = () => {
      const ctor = customElements.get('feedback-widget') as (CustomElementConstructor & { prototype: HTMLElement }) | undefined;
      if (!ctor) return false;
      const proto = ctor.prototype as unknown as Record<string, unknown>;
      const original = proto.attributeChangedCallback as (name: string, oldVal: string | null, newVal: string | null) => void;
      proto.attributeChangedCallback = function (this: HTMLElement, name: string, oldVal: string | null, newVal: string | null) {
        if (name === 'api-base') w.__attr.push({ from: oldVal, to: newVal, connected: this.isConnected });
        original.call(this, name, oldVal, newVal);
      };
      return true;
    };
    const timer = setInterval(() => { if (install()) clearInterval(timer); }, 5);
  });
}

export async function readAttributeProbe(page: Page) {
  return page.evaluate(() => (window as unknown as { __attr?: Array<{ from: string | null; to: string | null; connected: boolean }> }).__attr ?? []);
}

/**
 * 记录「节点被移除的**同一同步时刻**」的 DOM 状态（清理顺序的硬证据）。
 *
 * 内置 custom element reaction 不经过原型上的 `attributeChangedCallback` 属性查找，
 * 因此覆盖原型方法观察不到属性回调。改为拦截 `Element.prototype.remove`：
 * 我们的包装体在真正的移除发生**之前**同步执行，此时读取到的
 * 「api-base 属性是否已为空 / 令牌是否已清空」就是移除前的状态。
 */
export async function installRemovalProbe(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const w = window as unknown as { __removals: Array<Record<string, unknown>> };
    w.__removals = [];
    const original = Element.prototype.remove;
    Element.prototype.remove = function (this: Element) {
      if (this.tagName === 'FEEDBACK-WIDGET') {
        const el = this as unknown as Record<string, unknown>;
        const draft = (el.draft ?? {}) as { text?: string; screenshotBlob?: Blob | null };
        w.__removals.push({
          stillInDom: this.isConnected,
          apiBaseAttr: this.getAttribute('api-base'),
          accessToken: (el.accessToken as string | null) ?? null,
          draftText: String(draft.text ?? ''),
          draftBytes: draft.screenshotBlob ? draft.screenshotBlob.size : 0,
          identityEpoch: Number(el.identityEpoch ?? -1),
        });
      }
      return original.call(this);
    };
  });
}

export async function readRemovalProbe(page: Page) {
  return page.evaluate(() => (window as unknown as { __removals?: Array<Record<string, unknown>> }).__removals ?? []);
}

/** 读取反馈组件实例的身份相关状态。
 *
 * 这些字段是 TypeScript `private`（编译期可见性），运行时就是实例自有属性
 * （实测 `Object.getOwnPropertyNames(el)` 可列出 accessToken / draft / identityEpoch …）。
 * 用它们断言「令牌与草稿是否还在」比只看 DOM 更直接；配合 DOM 数量断言一起用。
 */
export interface WidgetState {
  present: boolean;
  apiBaseAttr: string | null;
  apiBase: string | null;
  appId: string | null;
  pageLabel: string | null;
  accessToken: string | null;
  tokenExpiresAt: number;
  idempotencyKey: string | null;
  lastFeedbackId: string | null;
  phase: string;
  identityEpoch: number;
  draftText: string;
  /** 草稿截图字节数（0 表示无截图）。 */
  draftBytes: number;
  /** 草稿截图字节的指纹（用于比对「同字节重试」）。 */
  draftBytesSignature: string;
  submitSnapshotIdempotencyKey: string | null;
  submitSnapshotText: string | null;
}

export async function readWidgetState(page: Page): Promise<WidgetState> {
  return page.evaluate(() => {
    const el = document.querySelector('feedback-widget') as unknown as Record<string, unknown> | null;
    if (!el) {
      return {
        present: false, apiBaseAttr: null, apiBase: null, appId: null, pageLabel: null,
        accessToken: null, tokenExpiresAt: 0, idempotencyKey: null, lastFeedbackId: null,
        phase: 'absent', identityEpoch: -1, draftText: '', draftBytes: 0, draftBytesSignature: '',
        submitSnapshotIdempotencyKey: null, submitSnapshotText: null,
      };
    }
    const draft = (el.draft ?? {}) as { text?: string; screenshotBlob?: Blob | null };
    const blob = draft.screenshotBlob ?? null;
    const snapshot = (el.submitSnapshot ?? null) as { idempotencyKey?: string; text?: string; screenshot?: Blob | null } | null;
    const snapshotBlob = snapshot?.screenshot ?? null;
    return {
      present: true,
      apiBaseAttr: (el as unknown as HTMLElement).getAttribute('api-base'),
      apiBase: (el.apiBase as string | null) ?? null,
      appId: (el.appId as string | null) ?? null,
      pageLabel: (el.pageLabel as string | null) ?? null,
      accessToken: (el.accessToken as string | null) ?? null,
      tokenExpiresAt: Number(el.tokenExpiresAt ?? 0),
      idempotencyKey: (el.idempotencyKey as string | null) ?? null,
      lastFeedbackId: (el.lastFeedbackId as string | null) ?? null,
      phase: String(el.phase ?? ''),
      identityEpoch: Number(el.identityEpoch ?? -1),
      draftText: String(draft.text ?? ''),
      draftBytes: blob ? blob.size : 0,
      draftBytesSignature: blob ? `${blob.size}:${blob.type}` : '',
      submitSnapshotIdempotencyKey: snapshot?.idempotencyKey ?? null,
      submitSnapshotText: snapshot?.text ?? null,
    };
  });
}

/** 读取一个「已脱离文档」的实例状态（登出后旧实例的唯一引用）。 */
export async function readDetachedState(page: Page, handle: string): Promise<{ accessToken: string | null; draftBytes: number; draftText: string; apiBaseAttr: string | null; identityEpoch: number } | null> {
  return page.evaluate((key) => {
    const el = (window as unknown as Record<string, unknown>)[key] as Record<string, unknown> | undefined;
    if (!el) return null;
    const draft = (el.draft ?? {}) as { text?: string; screenshotBlob?: Blob | null };
    return {
      accessToken: (el.accessToken as string | null) ?? null,
      draftBytes: draft.screenshotBlob ? draft.screenshotBlob.size : 0,
      draftText: String(draft.text ?? ''),
      apiBaseAttr: (el as unknown as HTMLElement).getAttribute('api-base'),
      identityEpoch: Number(el.identityEpoch ?? -1),
    };
  }, handle);
}

// ---------------------------------------------------------------- 元素定位

export function widget(page: Page) {
  return page.locator('feedback-widget');
}

/** 面板（未打开时带 hidden）。 */
export function panel(page: Page) {
  return page.locator('feedback-widget').locator('#fb-panel');
}

export function draftInput(page: Page) {
  return page.locator('feedback-widget').locator('#fb-textarea');
}

/** 等待反馈组件实例出现（已配置 + 会话就绪时才会创建）。 */
export async function waitForWidget(page: Page, timeout = 15_000) {
  await page.locator('feedback-widget').waitFor({ state: 'attached', timeout });
}

/** 打开反馈面板：点击灵感球入口（launcher-mode="orb"，见 实测 .fb-orb 可见而 tab 入口 display:none）。 */
export async function openPanel(page: Page) {
  await page.locator('feedback-widget').locator('button.fb-orb').first().click();
  await panel(page).waitFor({ state: 'visible' });
}

/** 关闭面板。 */
export async function closePanel(page: Page) {
  await page.locator('feedback-widget').locator('header button').first().click();
}

/** 在面板内输入草稿文字。 */
export async function typeDraft(page: Page, text: string) {
  await draftInput(page).fill(text);
}

/** 读取草稿文字。 */
export async function readDraft(page: Page): Promise<string> {
  return draftInput(page).inputValue();
}

/** 面板可见文案（用于行为断言，避免依赖内部结构）。 */
export async function panelText(page: Page): Promise<string> {
  return page.evaluate(() => document.querySelector('feedback-widget')?.shadowRoot?.textContent ?? '');
}

/** 截图区当前可见的按钮文案。 */
export async function visibleShotButtons(page: Page): Promise<string[]> {
  return page.locator('feedback-widget').locator('button:visible').allTextContents();
}

/** 点击底部主提交按钮。
 *  面板里存在多个「登录并提交」文案的按钮：底部主按钮 `button.fb-submit`、
 *  登录表单内的确认按钮 `button.fb-login-confirm`。按 class 定位才稳定。 */
export async function clickPrimarySubmit(page: Page) {
  await page.locator('feedback-widget').locator('button.fb-submit').first().click();
}

/** 组件内登录表单是否已展开。 */
export async function loginFormVisible(page: Page): Promise<boolean> {
  return page.locator('feedback-widget').locator('input[type="password"]:visible').count().then((n) => n > 0);
}

/** 在组件内登录表单里填账号密码（绝不使用真实凭据）。
 *  0.4.0 起面板内多了日志附件 input[type=file]，必须按 class 定位登录输入框。 */
export async function fillWidgetLogin(page: Page, username = 'tester', password = 'not-a-real-password') {
  const root = page.locator('feedback-widget');
  await root.locator('input.fb-login-username').fill(username);
  await root.locator('input.fb-login-password').fill(password);
}

/** 确认登录（登录表单内的 `button.fb-login-confirm`）。 */
export async function confirmWidgetLogin(page: Page) {
  await page.locator('feedback-widget').locator('button.fb-login-confirm').click();
}

/** 只展开组件内登录表单（不触发提交）。走组件公开 API `startLogin()`。 */
export async function openWidgetLoginForm(page: Page) {
  await page.evaluate(() => {
    const el = document.querySelector('feedback-widget') as unknown as { startLogin?: () => string } | null;
    el?.startLogin?.();
  });
  await page.locator('feedback-widget').locator('input[type="password"]:visible').waitFor({ state: 'visible' });
}

/** 在组件内登录表单里填账号密码并确认（不提交反馈）。 */
export async function loginInsideWidgetOnly(page: Page, username = 'tester', password = 'not-a-real-password') {
  await openWidgetLoginForm(page);
  await fillWidgetLogin(page, username, password);
  await confirmWidgetLogin(page);
}

/** 完整走一次「登录并提交」：先展开登录表单，登录成功后组件自动提交一次。 */
export async function loginAndSubmitInsideWidget(page: Page, username = 'tester', password = 'not-a-real-password') {
  await clickPrimarySubmit(page);
  await page.locator('feedback-widget').locator('input[type="password"]:visible').waitFor({ state: 'visible' });
  await fillWidgetLogin(page, username, password);
  await confirmWidgetLogin(page);
}

/**
 * 等待 viewport 自动截图真正写入草稿，返回写入后的组件状态。
 *
 * 宿主配置了 `capture-mode="viewport"`：打开面板会**异步**走
 * html2canvas → 遮挡验证 → PNG 编码，然后才把字节写进草稿。直接读 `draftBytes`
 * 会在这个流程完成前拿到 0（实测会得到 ~500–800ms 内的假失败）。
 *
 * 这里只是补上同步等待；断言本身仍是「截图字节必须存在且原样保留」，没有放宽。
 */
export async function waitForAutoScreenshot(page: Page, timeout = 20_000): Promise<WidgetState> {
  await expect
    .poll(async () => (await readWidgetState(page)).draftBytes, {
      timeout,
      message: 'viewport 模式呼出应产生截图字节（等自动截图完成）',
    })
    .toBeGreaterThan(0);
  return readWidgetState(page);
}
