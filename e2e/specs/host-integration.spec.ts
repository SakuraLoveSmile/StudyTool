import { expect, test } from '@playwright/test';
import {
  HOST_USER_A, HOST_USER_B, clickPrimarySubmit, draftInput, installRemovalProbe, loginInsideWidgetOnly, mockFeedbackService, mockHostApi,
  openPanel, closePanel, panelText, readDetachedState, readRemovalProbe, readWidgetState, readProbe,
  installProbe, typeDraft, waitForAutoScreenshot, waitForWidget, widget,
} from '../helpers/host-page';
import { deferred, feedbackRules, type MockResponse } from '../helpers/feedback-mock';

/**
 * 宿主集成级接入测试（已配置形态：VITE_FEEDBACK_API_BASE=http://127.0.0.1:5199）。
 *
 * 全部网络用 route 拦截：宿主 /api 不碰真实 backend，反馈服务不碰真实服务。
 * 身份与会话由 mockHostApi 的 state.user 控制，用于驱动世代切换。
 */

async function openConfiguredApp(page: import('@playwright/test').Page, rules: Parameters<typeof mockFeedbackService>[1] = []) {
  const state = { user: HOST_USER_A as typeof HOST_USER_A | null, csrfToken: null as string | null };
  const host = await mockHostApi(page, state);
  const svc = await mockFeedbackService(page, rules);
  await installProbe(page);
  await installRemovalProbe(page);
  await page.goto('/');
  await waitForWidget(page);
  return { state, host, svc };
}

test.describe('已配置形态：挂载门禁与公开属性', () => {
  test('会话就绪且允许展示外壳时挂载；公开属性取自构建期配置', async ({ page }) => {
    const { svc } = await openConfiguredApp(page);
    await expect(widget(page)).toHaveCount(1);

    const state = await readWidgetState(page);
    expect(state.apiBase).toBe('http://127.0.0.1:5199');
    expect(state.apiBaseAttr).toBe('http://127.0.0.1:5199');
    expect(state.appId).toBe('studytool');
    expect(state.pageLabel).toBe('today');
    // 未登录时不应有任何反馈服务请求。
    expect(svc.count()).toBe(0);
  });

  test('登录页不挂载实例（不加载组件界面、无入口）', async ({ page }) => {
    const { state } = await openConfiguredApp(page);
    state.user = null; // 会话失效 → 未认证
    await page.goto('/login');
    await expect(page.getByRole('button', { name: '登录' })).toBeVisible();
    await expect(widget(page)).toHaveCount(0);
  });
});

test.describe('路由保留草稿', () => {
  test('路由切换后草稿文字与截图仍在，实例不重建，page-label 更新', async ({ page }) => {
    const { svc } = await openConfiguredApp(page, [feedbackRules.loginOk(), feedbackRules.sessionOk()]);

    await openPanel(page);
    await typeDraft(page, '路由切换也要保留的草稿');
    // capture-mode="viewport"：呼出时自动截图，草稿里应已有截图字节。
    await expect(draftInput(page)).toHaveValue('路由切换也要保留的草稿');

    // viewport 自动截图是异步的：先等它真正写入草稿，再快照。
    const before = await waitForAutoScreenshot(page);
    expect(before.draftText).toBe('路由切换也要保留的草稿');
    expect(before.draftBytes, 'viewport 模式呼出应产生截图字节').toBeGreaterThan(0);
    expect(before.pageLabel).toBe('today');

    // 同一会话世代下切换路由（前端路由，不整页刷新）→ 必须是同一实例。
    await page.getByRole('link', { name: '资料与真题' }).first().click();
    await expect(page).toHaveURL(/\/materials$/, { timeout: 10_000 });

    const after = await readWidgetState(page);
    expect(after.present).toBe(true);
    // 草稿文字与截图字节都还在（实例未重建）。
    expect(after.draftText).toBe('路由切换也要保留的草稿');
    expect(after.draftBytes).toBe(before.draftBytes);
    expect(after.pageLabel).toBe('materials');
    // 仍未登录、未提交。
    expect(after.accessToken).toBeNull();
    expect(svc.countOf('POST', '/api/feedback')).toBe(0);
  });
});

test.describe('组件内独立登录', () => {
  test('登录在组件内完成：只打反馈服务，不依赖 RAG 会话', async ({ page }) => {
    const { host, svc } = await openConfiguredApp(page, [feedbackRules.loginOk(), feedbackRules.sessionOk()]);

    await openPanel(page);
    await typeDraft(page, '需要登录才能提交的内容');
    await loginInsideWidgetOnly(page, 'tester', 'not-a-real-password');

    await expect.poll(() => svc.countOf('POST', '/api/auth/login')).toBe(1);

    // 登录请求确实打到反馈服务（路径 + 显式 clientLabel/appId），且用的是测试凭据。
    const login = svc.all('POST', '/api/auth/login')[0];
    expect(login.pathname).toBe('/api/auth/login');
    const body = JSON.parse(login.bodyText) as { username: string; clientLabel: string; appId: string };
    expect(body.username).toBe('tester');
    expect(body.clientLabel).toBe('web:studytool');
    expect(body.appId).toBe('studytool');

    // 组件拿到了独立令牌（凭证在组件实例内存中；登录是异步的，需等待）。
    await expect.poll(async () => (await readWidgetState(page)).accessToken, { timeout: 10_000 }).toBe('mock-token-0001');
    const state = await readWidgetState(page);
    expect(state.accessToken).toBe('mock-token-0001');

    // RAG 宿主会话完全没被用于这件事：没有宿主登录请求，也没有宿主登出。
    expect(host.login, '不应调用宿主登录接口').toBe(0);
    expect(host.logout).toBe(0);
  });
});

test.describe('退出清理', () => {
  test('退出前先清空服务身份再移除实例：旧令牌与旧草稿不再存在', async ({ page }) => {
    const deferredSubmit = deferred<MockResponse>();
    const { svc } = await openConfiguredApp(page, [
      feedbackRules.loginOk(),
      feedbackRules.sessionOk(),
      { method: 'POST', path: '/api/feedback', handler: () => deferredSubmit.promise },
    ]);

    await openPanel(page);
    await typeDraft(page, '退出前的草稿');
    await loginInsideWidgetOnly(page, 'tester', 'not-a-real-password');
    await expect.poll(() => svc.countOf('POST', '/api/auth/login')).toBe(1);
    await expect.poll(async () => (await readWidgetState(page)).accessToken, { timeout: 10_000 }).toBe('mock-token-0001');

    // 提交被拿住（在途），保证草稿与令牌都处于「有值」状态。
    await clickPrimarySubmit(page);
    await expect.poll(() => svc.countOf('POST', '/api/feedback')).toBe(1);

    const before = await waitForAutoScreenshot(page);
    expect(before.accessToken).toBe('mock-token-0001');
    expect(before.draftText).toBe('退出前的草稿');
    expect(before.draftBytes).toBeGreaterThan(0);

    // 保留旧实例引用，登出后用于断言其状态确已失效。
    await page.evaluate(() => {
      (window as unknown as Record<string, unknown>).__oldWidget = document.querySelector('feedback-widget');
    });
    const oldEpoch = before.identityEpoch;

    await page.getByRole('button', { name: '退出' }).click();
    await expect(widget(page)).toHaveCount(0);

    // 关键顺序证据：包装后的 Element.prototype.remove 在真正移除**之前**同步执行，
    // 此刻读到的状态就是「移除前」的状态。
    const removals = (await readRemovalProbe(page)) as Array<{ stillInDom: boolean; apiBaseAttr: string | null; accessToken: string | null; draftText: string; draftBytes: number; identityEpoch: number }>;
    expect(removals.length, '应观察到反馈实例被移除').toBeGreaterThan(0);
    const removal = removals[removals.length - 1];
    expect(removal.stillInDom, '记录点必须发生在真正移除之前（isConnected=true）').toBe(true);
    expect(removal.apiBaseAttr, '移除前 api-base 必须已被置空（既不是旧地址，也不是哨兵值）').toBeNull();
    expect(removal.accessToken, '移除前令牌必须已失效').toBeNull();
    expect(removal.draftText, '移除前草稿文字必须已清空').toBe('');
    expect(removal.draftBytes, '移除前草稿截图字节必须已清空').toBe(0);
    expect(removal.identityEpoch, '移除前身份世代应已递增').toBeGreaterThan(oldEpoch);

    // 后果证据：旧实例的令牌与草稿已不存在。
    const detached = await readDetachedState(page, '__oldWidget');
    expect(detached).not.toBeNull();
    expect(detached!.apiBaseAttr, 'api-base 属性必须已被移除').toBeNull();
    expect(detached!.accessToken, '旧实例令牌必须已被清空').toBeNull();
    expect(detached!.draftText, '旧实例草稿文字必须已被清空').toBe('');
    expect(detached!.draftBytes, '旧实例草稿截图字节必须已被清空').toBe(0);
    expect(detached!.identityEpoch, '清理时身份世代应已递增').toBeGreaterThan(oldEpoch);

    // 释放被拿住的提交：旧实例已被清理，迟到响应不得产生任何副作用。
    deferredSubmit.resolve({ status: 201, body: { feedbackId: 'fb-late-logout', status: 'processing' } });
    await page.waitForTimeout(800);
    const probe = await readProbe(page);
    expect(probe.submitted, '旧实例已清理，迟到响应不得派发 feedback-submitted').toEqual([]);
    const afterRelease = await readDetachedState(page, '__oldWidget');
    expect(afterRelease!.accessToken, '迟到响应不得写回令牌').toBeNull();
    expect(afterRelease!.draftText, '迟到响应不得写回草稿').toBe('');
  });
});

test.describe('迟到响应不进入新会话', () => {
  test('身份世代切换后，旧服务迟到的提交响应被丢弃且不写入新实例', async ({ page }) => {
    const deferredSubmit = deferred<MockResponse>();
    const { svc, state } = await openConfiguredApp(page, [
      feedbackRules.loginOk(),
      feedbackRules.sessionOk(),
      { method: 'POST', path: '/api/feedback', handler: () => deferredSubmit.promise },
    ]);

    await openPanel(page);
    await typeDraft(page, '切世代前提交的内容');
    await loginInsideWidgetOnly(page, 'tester', 'not-a-real-password');
    await expect.poll(() => svc.countOf('POST', '/api/auth/login')).toBe(1);
    await expect.poll(async () => (await readWidgetState(page)).accessToken, { timeout: 10_000 }).toBe('mock-token-0001');

    await clickPrimarySubmit(page);
    await expect.poll(() => svc.countOf('POST', '/api/feedback')).toBe(1);
    const sent = svc.all('POST', '/api/feedback')[0];
    expect(sent.headers['authorization']).toBe('Bearer mock-token-0001');

    // 切换前快照：旧实例此刻确有令牌与草稿（否则「被丢弃」不可证）。
    const old = await readWidgetState(page);
    const oldEpoch = old.identityEpoch;
    expect(old.accessToken).toBe('mock-token-0001');
    expect(old.draftText).toBe('切世代前提交的内容');
    expect(old.lastFeedbackId, '提交在途时尚未写入反馈标识').toBeNull();

    // 身份/世代切换：登出销毁旧实例 → 换成另一个 RAG 用户建立新会话 → 新实例（服务地址不变）
    await page.getByRole('button', { name: '退出' }).click();
    await expect(widget(page)).toHaveCount(0);

    // 移除那一刻的顺序证据：移除前 api-base 已置空、令牌与草稿已失效。
    const removals = (await readRemovalProbe(page)) as Array<{ stillInDom: boolean; apiBaseAttr: string | null; accessToken: string | null; draftText: string; draftBytes: number; identityEpoch: number }>;
    expect(removals.length, '应观察到旧实例被移除').toBeGreaterThan(0);
    const removal = removals[removals.length - 1];
    expect(removal.stillInDom).toBe(true);
    expect(removal.apiBaseAttr).toBeNull();
    expect(removal.accessToken).toBeNull();
    expect(removal.draftText).toBe('');
    expect(removal.identityEpoch).toBeGreaterThan(oldEpoch);

    state.user = HOST_USER_B;
    state.csrfToken = 'csrf-rag-0002';
    await page.goto('/today');
    await waitForWidget(page);
    // 新实例尚未建立任何反馈服务请求（服务地址未变，但令牌与草稿都是新的）。
    const beforeRelease = svc.countOf('POST', '/api/feedback');
    expect(beforeRelease, '新实例不应有提交').toBe(1);

    // 现在才放行旧服务的迟到响应。
    deferredSubmit.resolve({ status: 201, body: { feedbackId: 'fb-late-0001', status: 'archived' } });
    await page.waitForTimeout(1200);

    const probe = await readProbe(page);
    expect(probe.submitted, '旧服务的迟到响应不得派发 feedback-submitted').toEqual([]);

    // 新实例：没有令牌、没有草稿、没有写入任何旧结果（迟到响应不得写入新实例状态）。
    const fresh = await readWidgetState(page);
    expect(fresh.present).toBe(true);
    expect(fresh.accessToken).toBeNull();
    expect(fresh.draftText).toBe('');
    expect(fresh.lastFeedbackId, '新实例不得写入旧反馈标识').toBeNull();
    expect(fresh.idempotencyKey).toBeNull();
    expect(fresh.phase).toBe('idle');
    expect(fresh.apiBaseAttr).toBe('http://127.0.0.1:5199');

    // 新实例没有额外提交（迟到响应不触发任何重放）。
    expect(svc.countOf('POST', '/api/feedback')).toBe(beforeRelease);
    expect(svc.countOf('POST', '/api/feedback')).toBe(1);
  });
});

test.describe('模拟故障：未保存时保留草稿', () => {
  test('提交未到达服务时保留草稿，重试复用同一幂等键与同一字节', async ({ page }) => {
    const { svc } = await openConfiguredApp(page, [
      feedbackRules.loginOk(),
      feedbackRules.sessionOk(),
      feedbackRules.submitServerError(),
    ]);

    await openPanel(page);
    await typeDraft(page, '网络故障也要留住的草稿');
    await loginInsideWidgetOnly(page, 'tester', 'not-a-real-password');
    await expect.poll(() => svc.countOf('POST', '/api/auth/login')).toBe(1);
    await expect.poll(async () => (await readWidgetState(page)).accessToken, { timeout: 10_000 }).toBe('mock-token-0001');

    await clickPrimarySubmit(page);
    await expect.poll(() => svc.countOf('POST', '/api/feedback')).toBe(1);
    await expect.poll(async () => (await panelText(page)).includes('重试提交'), { timeout: 10_000 }).toBe(true);

    // 草稿必须保留：文字与截图字节都还在。
    const failed = await waitForAutoScreenshot(page);
    expect(failed.draftText).toBe('网络故障也要留住的草稿');
    expect(failed.draftBytes).toBeGreaterThan(0);

    const first = svc.all('POST', '/api/feedback')[0];
    const firstKey = first.headers['idempotency-key'];

    // 草稿未变化的「重试提交」：同 key、同字节。
    await page.locator('feedback-widget').locator('button', { hasText: '重试提交' }).first().click();
    await expect.poll(() => svc.countOf('POST', '/api/feedback')).toBe(2);

    const retry = svc.all('POST', '/api/feedback')[1];
    expect(retry.headers['idempotency-key'], '重试必须复用同一幂等键').toBe(firstKey);
    // 截图字节必须完全一致（multipart 里同一段 PNG 字节）。
    const firstBytes = extractScreenshotBytes(first.bodyText);
    const retryBytes = extractScreenshotBytes(retry.bodyText);
    expect(firstBytes.length, '首次提交应带截图字节').toBeGreaterThan(0);
    expect(retryBytes).toEqual(firstBytes);
  });
});

test.describe('模拟故障：已保存但归档失败不重复提交', () => {
  test('归档失败只走只读刷新，绝不再次 POST', async ({ page }) => {
    const { svc } = await openConfiguredApp(page, [
      feedbackRules.loginOk(),
      feedbackRules.sessionOk(),
      feedbackRules.submitOk('fb-arch-1'),
      {
        method: 'GET',
        path: '/api/feedback/fb-arch-1',
        handler: [
          feedbackRules.recordArchiveFailed('fb-arch-1'), // 首次轮询 → 归档失败
          feedbackRules.recordArchived('fb-arch-1'),      // 手动刷新 → 归档完成
        ].map((rule) => (rule as { handler: never }).handler),
      },
    ]);

    await openPanel(page);
    await typeDraft(page, '归档失败也要保留原话');
    await loginInsideWidgetOnly(page, 'tester', 'not-a-real-password');
    await expect.poll(() => svc.countOf('POST', '/api/auth/login')).toBe(1);
    await expect.poll(async () => (await readWidgetState(page)).accessToken, { timeout: 10_000 }).toBe('mock-token-0001');

    await clickPrimarySubmit(page);
    await expect.poll(() => svc.countOf('POST', '/api/feedback')).toBe(1);

    // 首次轮询（2s 起）返回 failed → 进入归档失败相位，停止轮询。
    await expect.poll(() => svc.countOf('GET', '/api/feedback/fb-arch-1'), { timeout: 15_000 }).toBe(1);
    await expect.poll(async () => (await panelText(page)).includes('刷新状态'), { timeout: 10_000 }).toBe(true);

    const phase = await readWidgetState(page);
    expect(phase.phase).toBe('failed');
    const postsBeforeRefresh = svc.countOf('POST', '/api/feedback');

    // 手动「刷新状态」：只读 GET，绝不 POST。
    await page.locator('feedback-widget').locator('button', { hasText: '刷新状态' }).first().click();
    await expect.poll(() => svc.countOf('GET', '/api/feedback/fb-arch-1'), { timeout: 10_000 }).toBe(2);
    await expect.poll(async () => (await panelText(page)).includes('已归档'), { timeout: 10_000 }).toBe(true);

    expect(svc.countOf('POST', '/api/feedback'), '归档失败分支绝不再次 POST').toBe(postsBeforeRefresh);
    expect(svc.countOf('POST', '/api/feedback')).toBe(1);
    // 刷新得到的记录已归档。
    const after = await readWidgetState(page);
    expect(after.phase).toBe('archived');
    expect(after.lastFeedbackId).toBe('fb-arch-1');
  });
});

test.describe('主题切换不干扰反馈实例', () => {
  test('切换深色只更新组件 theme 属性：草稿、截图与实例保持不变', async ({ page }) => {
    await openConfiguredApp(page, [feedbackRules.loginOk(), feedbackRules.sessionOk()]);
    await openPanel(page);
    await typeDraft(page, '切换主题后草稿仍在');
    const before = await waitForAutoScreenshot(page);
    expect(before.draftBytes).toBeGreaterThan(0);

    // 给当前实例打上标记：若主题切换重建了实例，标记会随旧节点一起消失。
    await page.evaluate(() => {
      (document.querySelector('feedback-widget') as unknown as Record<string, unknown>).__themeMarker = 'kept';
    });

    // 先关面板再点导航栏：面板层级高于下拉菜单，草稿留在实例内不受关合影响。
    await closePanel(page);
    await page.getByRole('button', { name: /外观设置/ }).click();
    await page.getByRole('menuitemradio', { name: '深色' }).click();
    await expect.poll(() => page.evaluate(() => document.documentElement.dataset.theme)).toBe('dark');

    const after = await readWidgetState(page);
    expect(after.present).toBe(true);
    expect(after.draftText).toBe('切换主题后草稿仍在');
    expect(after.draftBytes).toBe(before.draftBytes);
    // 同一实例：theme 只是公开属性赋值，不走重建路径。
    expect(await page.evaluate(() => (document.querySelector('feedback-widget') as unknown as Record<string, unknown>).__themeMarker)).toBe('kept');
    // 组件 theme 属性同步为实际主题（组件据此切换自身深浅样式）。
    await expect
      .poll(() => page.evaluate(() => document.querySelector('feedback-widget')?.getAttribute('theme')))
      .toBe('dark');
  });
});

/** 从 multipart 请求体里取出截图字节（PNG 段）。 */
function extractScreenshotBytes(body: string): string {
  const marker = /name="screenshot"[\s\S]*?\r?\n\r?\n([\s\S]*?)\r?\n--/;
  const match = body.match(marker);
  return match ? match[1] : '';
}
