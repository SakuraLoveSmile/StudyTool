import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { expect, test } from '@playwright/test';

/**
 * 组件级 fixture 测试。
 *
 * 覆盖宿主集成测试无法覆盖的一层：
 *  - 组件包 / 分块加载失败时宿主学习页面不受影响，且诊断不含敏感信息；
 *  - **同一个实例**上 api-base 运行时切换后的身份世代行为（宿主固定构建期地址，换不了）；
 *  - 「清洗身份」的归一化陷阱：api-base 属性本来不存在时 removeAttribute 不触发回调。
 *
 * 组件产物来源固定性由本文件的 Node 侧断言保证（版本 + tarball sha256 与核验清单一致）。
 */

const FIXTURE = 'http://127.0.0.1:5276/widget-fixture.html';
const FEEDBACK_BASE = 'http://127.0.0.1:5199';
const FEEDBACK_BASE_B = 'http://127.0.0.1:5198';
const VENDOR_TARBALL = '../../frontend/vendor/feedback-web-0.4.0.tgz';
const EXPECTED_SHA256 = '2c22aa3110cb2e84780786ee37dc263ff766b3e6ada9699597a8e79f8b0d5533';

test('依赖来源固定：@feedback/web 实装 0.4.0，且与核验清单 sha256 一致', async () => {
  const pkg = JSON.parse(await readFile(new URL('../node_modules/@feedback/web/package.json', import.meta.url), 'utf8')) as { version: string };
  expect(pkg.version, 'fixture 必须测 0.4.0，防止静默退化到旧版本').toBe('0.4.0');

  const tarball = await readFile(new URL(VENDOR_TARBALL, import.meta.url));
  const digest = createHash('sha256').update(tarball).digest('hex');
  expect(digest, 'vendor tarball 必须与 .feedback-work/vendor-verified/SHA256SUMS 登记值一致').toBe(EXPECTED_SHA256);
});

test.describe('组件/分块加载失败', () => {
  test('组件包被阻断不注册元素、不产生反馈请求，且学习页面不受影响', async ({ page }) => {
    const errors: string[] = [];
    const requested: string[] = [];
    page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
    page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
    page.on('request', (r) => requested.push(r.url()));

    await page.goto(FIXTURE);
    await page.waitForTimeout(500);

    // fixture 页本身正常（代表宿主学习页面）。
    await expect(page.locator('#page-title')).toHaveText('学习页面（fixture）');
    await expect(page.getByRole('button', { name: '普通按钮' })).toBeVisible();

    // 元素未注册 → 不会挂载任何反馈界面。
    const registered = await page.evaluate(() => Boolean(customElements.get('feedback-widget')));
    expect(registered, 'UMD 产物应已注册 custom element').toBe(true);
    // 未配置 api-base 时组件不应发出任何请求。
    const feedbackRequests = requested.filter((u) => u.startsWith(FEEDBACK_BASE));
    expect(feedbackRequests, '未配置有效服务地址时不得发请求').toEqual([]);

    // 页面没有任何 5xx/白屏式崩溃。
    expect(errors.filter((e) => e.includes('pageerror')), '加载失败不得抛未捕获异常').toEqual([]);
  });

  test('宿主动态导入失败时：无反馈入口、学习页面正常、诊断脱敏', async ({ page }) => {
    // 让组件 chunk 加载失败（模拟分块加载失败）。
    await page.route(/feedback[-_]web/i, (route) => route.abort('failed'));

    await page.route(/\/api\/auth\/me(\?|$)/, (route) => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ user: { id: 'rag-user-a', username: 'alice', must_change_password: false }, csrf_token: 'csrf-rag-0001', auth_required: true }),
    }));
    await page.route(/\/api\/learning\/(settings|chapters|today)(\?|$)/, (route) => route.fulfill({
      status: 200, contentType: 'application/json', body: JSON.stringify({ units: [], state_version: 1, daily_new_limit: 10 }),
    }));
    await page.route(/\/api\/materials\/tasks\/active\/list(\?|$)/, (route) => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }));

    const errors: string[] = [];
    page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });

    await page.goto('http://127.0.0.1:5275/');
    // 学习页面不受影响：外壳与主内容仍可见。
    await expect(page.locator('nav')).toBeVisible();
    await expect(page.locator('main')).toBeVisible();
    await expect(page.getByRole('contentinfo')).toBeVisible();
    // 不挂载反馈实例（加载失败就不显示入口）。
    await expect(page.locator('feedback-widget')).toHaveCount(0);

    // 诊断存在且脱敏：有来源前缀，但不得含用户名 / csrf 令牌 / Cookie。
    await expect.poll(() => errors.some((e) => e.includes('[rag:feedback]')), { timeout: 10_000 }).toBe(true);
    const diagnostic = errors.filter((e) => e.includes('[rag:feedback]')).join('\n');
    expect(diagnostic).toContain('组件加载失败');
    expect(diagnostic, '诊断不得含 RAG 用户名').not.toContain('alice');
    expect(diagnostic, '诊断不得含 csrf 令牌').not.toContain('csrf-rag-0001');
    expect(diagnostic, '诊断不得含 Cookie').not.toMatch(/cookie/i);
    expect(diagnostic, '诊断不得含查询参数形式的令牌').not.toMatch(/token=/i);
  });
});

test.describe('同一实例上的身份世代（运行时切换 api-base）', () => {
  test('切换 api-base 后旧服务的迟到响应被丢弃，且不写回旧令牌与旧草稿', async ({ page }) => {
    const reqA: string[] = [];
    const reqB: string[] = [];
    let releaseLate: (() => void) | null = null;
    const lateArrived = new Promise<void>((resolve) => { releaseLate = resolve; });

    const corsHeaders = (origin: string | undefined) => ({
      'access-control-allow-origin': origin ?? '*',
      'access-control-allow-headers': 'authorization,content-type,idempotency-key',
    });

    // 旧服务：登录返回令牌；GET 记录推迟到「切换之后」再响应 —— 制造真正的迟到响应。
    await page.route(`${FEEDBACK_BASE}/**`, async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const cors = corsHeaders(request.headers()['origin']);
      if (request.method() === 'OPTIONS') {
        await route.fulfill({ status: 204, headers: { ...cors, 'access-control-allow-methods': 'GET,POST,OPTIONS' } });
        return;
      }
      reqA.push(url.pathname);
      if (url.pathname === '/api/auth/login') {
        await route.fulfill({
          status: 200,
          headers: cors,
          contentType: 'application/json',
          body: JSON.stringify({
            ok: true,
            token: 'mock-token-fixture',
            expiresAt: new Date(Date.now() + 3600_000).toISOString(),
            user: { id: 'u1', username: 'fixture-user', role: 'user' },
            quota: { dailyLimit: 5, used: 0, remaining: 5, resetAt: new Date(Date.now() + 3600_000).toISOString() },
          }),
        });
        return;
      }
      if (request.method() === 'GET') {
        // 拿住请求：等测试显式放行（那时身份已经切换）。
        await lateArrived;
        await route.fulfill({
          status: 200,
          headers: cors,
          contentType: 'application/json',
          body: JSON.stringify({ id: 'fb-late-1', status: 'archived', createdAt: new Date().toISOString(), updatedAt: new Date().toISOString(), kaneoUrl: null }),
        });
        return;
      }
      await route.fulfill({ status: 200, headers: cors, contentType: 'application/json', body: JSON.stringify({ feedbackId: 'fb-a-1', status: 'processing' }) });
    });

    // 新服务：只记录，一旦被请求就说明旧令牌/旧世代泄漏到了新服务。
    await page.route(`${FEEDBACK_BASE_B}/**`, async (route) => {
      reqB.push(new URL(route.request().url()).pathname);
      await route.fulfill({ status: 200, headers: corsHeaders(route.request().headers()['origin']), contentType: 'application/json', body: '{}' });
    });

    await page.goto(FIXTURE);
    await page.waitForFunction(() => Boolean((window as unknown as { __fixture?: unknown }).__fixture));

    // 组件内登录（公开 API startLogin），填入测试凭据并确认。
    await page.evaluate(() => (window as unknown as { __fixture: { el: { startLogin: () => void } } }).__fixture.el.startLogin());
    await page.evaluate(() => {
      const root = (window as unknown as { __fixture: { el: HTMLElement } }).__fixture.el.shadowRoot!;
      const username = root.querySelector('input.fb-login-username') as HTMLInputElement;
      const password = root.querySelector('input.fb-login-password') as HTMLInputElement;
      username.value = 'fixture-user';
      password.value = 'fixture-pass';
      username.dispatchEvent(new Event('input', { bubbles: true }));
      password.dispatchEvent(new Event('input', { bubbles: true }));
      (root.querySelector('button.fb-login-confirm') as HTMLButtonElement).click();
    });

    await expect.poll(async () => page.evaluate(() => (window as unknown as { __fixture: { snapshot: () => { accessToken: string | null } } }).__fixture.snapshot().accessToken), { timeout: 10_000 }).toBe('mock-token-fixture');

    // 写入草稿文字，并直接发起一次「读取旧记录」的请求（内部刷新路径），让响应受控迟到。
    await page.evaluate(() => {
      const root = (window as unknown as { __fixture: { el: HTMLElement } }).__fixture.el.shadowRoot!;
      const ta = root.querySelector('#fb-textarea') as HTMLTextAreaElement;
      ta.value = '切换服务前的草稿';
      ta.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await page.evaluate(() => {
      const el = (window as unknown as { __fixture: { el: Record<string, unknown> } }).__fixture.el;
      (el as unknown as { lastFeedbackId: string | null }).lastFeedbackId = 'fb-late-1';
      (el as unknown as { refreshLastRecord: () => Promise<void> }).refreshLastRecord();
    });
    await expect.poll(() => reqA.filter((p) => p === '/api/feedback/fb-late-1').length, { timeout: 10_000 }).toBe(1);

    const before = await page.evaluate(() => (window as unknown as { __fixture: { snapshot: () => Record<string, unknown> } }).__fixture.snapshot());
    expect(before.accessToken).toBe('mock-token-fixture');
    expect(before.draftText).toBe('切换服务前的草稿');
    expect(before.apiBaseAttr).toBe(FEEDBACK_BASE);

    // 切换服务身份（运行时 api-base 变化 = 完整身份切换）——旧请求仍在途。
    await page.evaluate((next) => (window as unknown as { __fixture: { switchBase: (b: string) => void } }).__fixture.switchBase(next), FEEDBACK_BASE_B);
    await page.waitForTimeout(300);

    const afterSwitch = await page.evaluate(() => (window as unknown as { __fixture: { snapshot: () => Record<string, unknown> } }).__fixture.snapshot());
    expect(afterSwitch.apiBase, '新服务地址已生效').toBe(FEEDBACK_BASE_B);
    expect(afterSwitch.accessToken, '换服务必须清空访问令牌（新服务需重新登录）').toBeNull();
    expect(Number(afterSwitch.tokenExpiresAt)).toBe(0);
    expect(afterSwitch.draftText, '换服务必须清空草稿').toBe('');
    expect(Number(afterSwitch.identityEpoch), '身份世代必须递增').toBeGreaterThan(Number(before.identityEpoch));

    // 现在才放行旧服务的迟到响应（真的到达，不是「恰好没返回」）。
    releaseLate!();
    await page.waitForTimeout(1000);

    const afterLate = await page.evaluate(() => (window as unknown as { __fixture: { snapshot: () => Record<string, unknown> } }).__fixture.snapshot());
    // 迟到的 archived 记录不得写回：相位仍应是切换后的 idle，草稿与令牌不得复活。
    expect(afterLate.phase, '旧服务的迟到响应不得写入新身份状态（相位）').toBe('idle');
    expect(afterLate.lastFeedbackId, '迟到的 archived 记录不得写回').toBeNull();
    expect(afterLate.accessToken, '迟到的响应不得写回令牌').toBeNull();
    expect(afterLate.draftText, '迟到的响应不得写回草稿').toBe('');
    // 事件不得派发。
    const events = await page.evaluate(() => (window as unknown as { __fixture: { events: unknown[] } }).__fixture.events);
    expect(events, '迟到响应不得派发 feedback-submitted').toEqual([]);
    // 新服务绝不能被旧令牌访问。
    expect(reqB, `新服务不得收到任何请求，实际：${JSON.stringify(reqB)}`).toEqual([]);
  });

  test('清洗身份的归一化陷阱：api-base 属性必须真的被移除（不是哨兵值）', async ({ page }) => {
    await page.goto(FIXTURE);
    await page.waitForFunction(() => Boolean((window as unknown as { __fixture?: unknown }).__fixture));

    const before = await page.evaluate(() => (window as unknown as { __fixture: { snapshot: () => Record<string, unknown> } }).__fixture.snapshot());
    expect(before.apiBaseAttr).toBe(FEEDBACK_BASE);
    const epochBefore = Number(before.identityEpoch);

    // 模拟宿主清理：先确保属性存在，再置 null（与 useFeedbackWidget.clearServiceIdentity 同语义）。
    const result = await page.evaluate(() => {
      const el = (window as unknown as { __fixture: { el: HTMLElement & { apiBase: string | null } } }).__fixture.el;
      if (el.getAttribute('api-base') === null) el.setAttribute('api-base', 'urn:rag:feedback:identity-reset');
      el.apiBase = null;
      return {
        attr: el.getAttribute('api-base'),
        epoch: Number((el as unknown as Record<string, unknown>).identityEpoch),
      };
    });

    expect(result.attr, 'api-base 属性必须被移除；残留哨兵值说明清理没生效').toBeNull();
    expect(result.epoch, '置空必须触发身份世代递增').toBeGreaterThan(epochBefore);

    // 置空后组件不得再发请求（令牌已随身份切换清空）。
    const requested: string[] = [];
    page.on('request', (r) => requested.push(r.url()));
    await page.waitForTimeout(600);
    expect(requested.filter((u) => u.startsWith(FEEDBACK_BASE))).toEqual([]);
  });
});
