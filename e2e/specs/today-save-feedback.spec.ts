import { expect, test, type Locator, type Page, type Route } from '@playwright/test';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { deferred, type Deferred } from '../helpers/feedback-mock';

/**
 * TodayLearningView「保存反馈」契约测试 —— /today 右侧「学习范围」「今日设置」两张卡片。
 *
 * 冻结契约（UI 实现并行推进，断言只依赖契约面，不依赖具体 DOM 标签）：
 * - 两张卡片各有 h2 标题（学习范围 / 今日设置）与一个「保存」按钮；保存中显示「保存中…」；
 * - 成功消息只出现在**同一张卡片内**的 role="status"：学习范围已保存 / 今日设置已保存；
 * - 失败只出现在同一张卡片内的 role="alert"（可操作消息）；
 * - 范围 PUT 成功但随后的 GET /api/learning/today 失败 → 同卡 role="alert" 警示
 *   （文案同时含「学习范围已保存」与「刷新失败」）+「重试刷新」按钮；
 *   点击它只允许补发 GET /api/learning/today，绝不再发 PUT /learning/scope；
 * - 一张卡保存中只禁用**本卡**输入，另一张卡不受影响；
 * - 保存成功后再次编辑 → 成功消息消失，回到未保存态。
 *
 * 网络面：单一 page.route 按 **pathname 前缀 /api/** 拦截 —— 声明的端点 fulfillment，
 * 未声明的一律 route.abort() 并记入 mock.aborted（可诊断），因此 vite 的
 * /api → 127.0.0.1:8000 代理永不被触达，本套件不需要真实 backend。
 * 注意不能用 glob '**\/api/**'：它会误伤 Vite 模块路径 /src/api/client.ts
 * （实测该请求被 abort 后整页空白）。
 */

// ---------------------------------------------------------------- 固定 mock 数据

const SCOPE_TITLE = '学习范围';
const SETTINGS_TITLE = '今日设置';
const SCOPE_SAVED = '学习范围已保存';
const SETTINGS_SAVED = '今日设置已保存';

/** auth_required:false → 路由守卫直接放行 /today，无需真实登录。 */
const AUTH_ME_BODY = { auth_required: false, user: null, csrf_token: null };

const SETTINGS_BODY = {
  daily_new_limit: 10,
  daily_review_limit: 20,
  reveal_after_seconds: null,
  auto_advance: false,
  show_hints: true,
  timezone: 'Asia/Shanghai',
};

const TODAY_BODY = {
  status: 'idle',
  round: null,
  total: 0,
  completed: 0,
  remaining: 0,
  new_count: 0,
  review_count: 0,
  queue: [],
  current_attempt: null,
  current_card: null,
  override_enabled: false,
  state_version: 'v1',
  scope: { chapter_ids: ['ch-1'], include_unassigned: true },
  settings: { ...SETTINGS_BODY },
};

const CHAPTERS_BODY = [
  { id: 'ch-1', name: '第一章 示例', sort_order: 0, enabled: true, card_count: 3 },
  { id: 'ch-2', name: '第二章 测试', sort_order: 1, enabled: true, card_count: 5 },
];

/**
 * TaskDrawer 挂载时会拉取一次任务历史（抽屉关闭态，其失败不影响 /today 本页）。
 * 它是本页唯一「预期内」的未声明 /api 请求；其余被 abort 的请求会在断言里现形。
 */
const EXPECTED_ABORTED_PATH = '/api/materials/tasks/history';

// ---------------------------------------------------------------- /api mock

interface ApiRequestRecord {
  method: string;
  pathname: string;
  url: string;
  bodyText: string;
}

interface TodayApiMock {
  /** 全部 /api 请求（含被 abort 的），按到达顺序记录。 */
  requests: ApiRequestRecord[];
  /** 未声明而被 abort 的请求（诊断用）。 */
  aborted: ApiRequestRecord[];
  controls: {
    /** 置 true 后对应端点返回 500 {"detail":"boom"}。 */
    failTodayGet: boolean;
    failSettingsPut: boolean;
    failScopePut: boolean;
    /**
     * 门闩：置位后对应端点挂起、resolve 后才返回。
     * 用于确定性观测「保存中…」中间态与并发保存（比固定 sleep 可靠，不会引入时序抖动）。
     */
    gates: {
      todayGet?: Deferred<void>;
      settingsPut?: Deferred<void>;
      scopePut?: Deferred<void>;
    };
  };
  /** 某 method + pathname 的请求次数。 */
  count(method: string, pathname: string): number;
}

const parseJson = (text: string): Record<string, unknown> => {
  try {
    const parsed: unknown = JSON.parse(text || '{}');
    return parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : {};
  } catch {
    return {};
  }
};

async function installTodayApi(page: Page): Promise<TodayApiMock> {
  const mock: TodayApiMock = {
    requests: [],
    aborted: [],
    controls: { failTodayGet: false, failSettingsPut: false, failScopePut: false, gates: {} },
    count(method, pathname) {
      return mock.requests.filter((r) => r.method === method && r.pathname === pathname).length;
    },
  };

  const json = (route: Route, status: number, body: unknown) =>
    route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

  // 按 pathname 前缀匹配 /api/**：避免 glob '**/api/**' 误伤 Vite 模块请求
  // （/src/api/client.ts 的 pathname 含 /api/，被 abort 会让整页空白）。
  await page.route((url) => url.pathname.startsWith('/api/'), async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const record: ApiRequestRecord = {
      method: request.method().toUpperCase(),
      pathname: url.pathname,
      url: request.url(),
      bodyText: request.postData() ?? '',
    };
    mock.requests.push(record);
    const { method, pathname } = record;
    const posted = () => parseJson(record.bodyText);

    if (method === 'GET' && pathname === '/api/auth/me') {
      await json(route, 200, AUTH_ME_BODY);
      return;
    }
    if (method === 'GET' && pathname === '/api/learning/today') {
      if (mock.controls.gates.todayGet) await mock.controls.gates.todayGet.promise;
      await json(
        route,
        mock.controls.failTodayGet ? 500 : 200,
        mock.controls.failTodayGet ? { detail: 'boom' } : TODAY_BODY,
      );
      return;
    }
    if (method === 'GET' && pathname === '/api/learning/settings') {
      await json(route, 200, { ...SETTINGS_BODY });
      return;
    }
    if (method === 'GET' && pathname === '/api/learning/chapters') {
      await json(route, 200, CHAPTERS_BODY);
      return;
    }
    if (method === 'PUT' && pathname === '/api/learning/settings') {
      if (mock.controls.gates.settingsPut) await mock.controls.gates.settingsPut.promise;
      if (mock.controls.failSettingsPut) {
        await json(route, 500, { detail: 'boom' });
      } else {
        // 回声：服务端持久化 posted 设置并补上 timezone。
        await json(route, 200, { ...posted(), timezone: 'Asia/Shanghai' });
      }
      return;
    }
    if (method === 'PUT' && pathname === '/api/learning/scope') {
      if (mock.controls.gates.scopePut) await mock.controls.gates.scopePut.promise;
      if (mock.controls.failScopePut) {
        await json(route, 500, { detail: 'boom' });
      } else {
        const body = posted();
        await json(route, 200, {
          chapter_ids: Array.isArray(body.chapter_ids) ? body.chapter_ids : [],
          include_unassigned: typeof body.include_unassigned === 'boolean' ? body.include_unassigned : true,
        });
      }
      return;
    }

    // 未声明的 /api 调用：绝不放行（避免漏到真实 backend），记录下来供断言/诊断。
    mock.aborted.push(record);
    await route.abort();
  });

  return mock;
}

// ---------------------------------------------------------------- 定位器

/**
 * 卡片容器 = 「包含本卡 h2、但不含另一张卡 h2」的最外层祖先。
 * 与标签名无关（section/div/article 皆可）：两张卡是两棵互不包含的子树，
 * 满足条件的最外层元素即卡片根 —— 后续 status/alert 断言因此天然收敛在卡内、绝不串卡。
 */
function cardRoot(page: Page, title: string, otherTitle: string): Locator {
  return page
    .locator(`xpath=//*[.//h2[normalize-space()="${title}"] and not(.//h2[normalize-space()="${otherTitle}"])]`)
    .first();
}

const scopeCard = (page: Page): Locator => cardRoot(page, SCOPE_TITLE, SETTINGS_TITLE);
const settingsCard = (page: Page): Locator => cardRoot(page, SETTINGS_TITLE, SCOPE_TITLE);
/** 「保存」/「保存中…」是同一个按钮：正则覆盖两种文案（精确断言另用 anchored regex）。 */
const saveButton = (card: Locator): Locator => card.getByRole('button', { name: /保存/ });
const cardStatus = (card: Locator): Locator => card.getByRole('status');
const cardAlert = (card: Locator): Locator => card.getByRole('alert');
const retryRefreshButton = (card: Locator): Locator => card.getByRole('button', { name: '重试刷新' });
const newLimitInput = (page: Page): Locator => settingsCard(page).getByLabel(/每日新卡上限/);
const reviewLimitInput = (page: Page): Locator => settingsCard(page).getByLabel(/每日复习上限/);
const autoAdvanceInput = (page: Page): Locator => settingsCard(page).getByLabel(/评分后自动进入下一张/);
const showHintsInput = (page: Page): Locator => settingsCard(page).getByLabel(/揭示前显示提示/);
const chapterCheckbox = (page: Page, id: string): Locator =>
  scopeCard(page).locator(`input[type="checkbox"][value="${id}"]`);
const unassignedCheckbox = (page: Page): Locator => scopeCard(page).getByLabel(/包含未分类卡片/);

/** 打开 /today 并等到初始数据真正落进输入框（避免「刚渲染就读值」的假阴性）。 */
async function openToday(page: Page): Promise<TodayApiMock> {
  const api = await installTodayApi(page);
  await page.goto('/today');
  await expect(page.getByRole('heading', { name: SCOPE_TITLE, exact: true }), '学习范围卡片应渲染').toBeVisible();
  await expect(page.getByRole('heading', { name: SETTINGS_TITLE, exact: true }), '今日设置卡片应渲染').toBeVisible();
  // 初始 loadPage 三个 GET 已完成：勾选态与设置值均来自 mock 响应。
  await expect(chapterCheckbox(page, 'ch-1'), 'ch-1 应由 scope.chapter_ids 选中').toBeChecked();
  await expect(chapterCheckbox(page, 'ch-2')).not.toBeChecked();
  await expect(newLimitInput(page)).toHaveValue('10');
  await expect(reviewLimitInput(page)).toHaveValue('20');
  return api;
}

/** 用真实 Tab / Shift+Tab 按键把焦点移到目标控件（有界步数内必须到达）。 */
async function tabToFocus(page: Page, target: Locator, options: { backward?: boolean; maxPresses?: number } = {}): Promise<void> {
  const key = options.backward ? 'Shift+Tab' : 'Tab';
  const max = options.maxPresses ?? 80;
  for (let i = 0; i < max; i += 1) {
    const focused = await target.evaluate((el) => el === document.activeElement).catch(() => false);
    if (focused) return;
    await page.keyboard.press(key);
  }
  await expect(target, `按 ${key} 应能聚焦到目标控件`).toBeFocused();
}

/** spec 内显式截图目录：e2e/test-results-today-save/（相对本文件定位，不依赖 cwd）。 */
const SCREENSHOT_DIR = fileURLToPath(new URL('../test-results-today-save/', import.meta.url));
const shotPath = (name: string): string => join(SCREENSHOT_DIR, name);

// ---------------------------------------------------------------- 用例

test.describe('今日页保存反馈（学习范围 / 今日设置）', () => {
  test('范围保存成功：同卡 status 显示「学习范围已保存」，另一卡与页级 alert 静默', async ({ page }) => {
    const api = await openToday(page);
    const scope = scopeCard(page);
    const settings = settingsCard(page);

    await chapterCheckbox(page, 'ch-2').check();
    await saveButton(scope).click();

    await expect(cardStatus(scope), '范围卡内应出现成功 status').toContainText(SCOPE_SAVED);
    expect(api.count('PUT', '/api/learning/scope'), '范围保存应只发一次 PUT').toBe(1);
    const put = api.requests.find((r) => r.method === 'PUT' && r.pathname === '/api/learning/scope');
    const body = parseJson(put?.bodyText ?? '');
    expect([...(body.chapter_ids as string[])].sort(), 'PUT 应携带勾选后的章节集合').toEqual(['ch-1', 'ch-2']);
    expect(body.include_unassigned).toBe(true);

    // 契约：范围保存成功后会刷新今日队列（初始 loadPage 一次 + 保存后一次）。
    expect(api.count('GET', '/api/learning/today')).toBe(2);

    await expect(cardAlert(scope)).toHaveCount(0);
    await expect(cardStatus(settings), '设置卡不应出现任何状态').toHaveCount(0);
    await expect(cardAlert(settings)).toHaveCount(0);
    await expect(page.getByRole('alert'), '全页不应残留 alert（含页级错误条）').toHaveCount(0);

    // 未声明请求诊断：除 TaskDrawer 的任务历史外，不应有别的 /api 调用被 abort。
    const unexpected = api.aborted.filter((r) => r.pathname !== EXPECTED_ABORTED_PATH);
    expect(
      unexpected.map((r) => `${r.method} ${r.url}`),
      '出现未声明的 /api 调用（已被 abort；需补进 mock 或修复页面）',
    ).toEqual([]);
  });

  test('设置保存成功：仅设置卡内 status 显示「今日设置已保存」', async ({ page }) => {
    const api = await openToday(page);
    const settings = settingsCard(page);
    const scope = scopeCard(page);

    await newLimitInput(page).fill('25');
    await saveButton(settings).click();

    await expect(cardStatus(settings)).toContainText(SETTINGS_SAVED);
    expect(api.count('PUT', '/api/learning/settings')).toBe(1);
    const put = api.requests.find((r) => r.method === 'PUT' && r.pathname === '/api/learning/settings');
    expect(parseJson(put?.bodyText ?? ''), 'PUT 应携带编辑后的设置值').toMatchObject({
      daily_new_limit: 25,
      daily_review_limit: 20,
      auto_advance: false,
      show_hints: true,
    });

    await expect(cardStatus(scope), '范围卡不应出现任何状态').toHaveCount(0);
    await expect(cardAlert(scope)).toHaveCount(0);
    await expect(cardAlert(settings)).toHaveCount(0);
    await expect(page.getByRole('alert')).toHaveCount(0);
  });

  test('设置保存失败：同卡 alert、草稿保留、再次保存会重发 PUT', async ({ page }) => {
    const api = await openToday(page);
    const settings = settingsCard(page);

    api.controls.failSettingsPut = true;
    await newLimitInput(page).fill('42');
    await saveButton(settings).click();

    const alert = cardAlert(settings);
    await expect(alert, '设置卡内应出现错误 alert').toBeVisible();
    await expect(alert).toHaveAttribute('role', 'alert');
    await expect(alert, '错误消息必须非空（可操作文案由实现决定，但不得为空）').toHaveText(/\S+/);
    await expect(cardStatus(settings), '失败时不得误报成功').toHaveCount(0);
    expect(api.count('PUT', '/api/learning/settings')).toBe(1);

    // 草稿保留：失败不得回滚输入值。
    await expect(newLimitInput(page)).toHaveValue('42');

    // 后端恢复后再点保存 → 重发 PUT（计数 +1），且仍携带草稿值。
    api.controls.failSettingsPut = false;
    await saveButton(settings).click();
    await expect(cardStatus(settings)).toContainText(SETTINGS_SAVED);
    expect(api.count('PUT', '/api/learning/settings'), '再次保存应重发 PUT').toBe(2);
    const puts = api.requests.filter((r) => r.method === 'PUT' && r.pathname === '/api/learning/settings');
    expect(parseJson(puts[1].bodyText)).toMatchObject({ daily_new_limit: 42 });
  });

  test('保存中态：按钮变「保存中…」且禁用、本卡输入禁用、另一张卡不受影响', async ({ page }) => {
    const api = await openToday(page);
    const settings = settingsCard(page);
    const scope = scopeCard(page);

    api.controls.gates.settingsPut = deferred<void>();
    await newLimitInput(page).fill('30');
    await saveButton(settings).click();

    await expect(saveButton(settings)).toHaveText(/^保存中…$/);
    await expect(saveButton(settings)).toBeDisabled();
    await expect(newLimitInput(page)).toBeDisabled();
    await expect(reviewLimitInput(page)).toBeDisabled();
    await expect(autoAdvanceInput(page)).toBeDisabled();
    await expect(showHintsInput(page)).toBeDisabled();
    // 中间态不得提前出现结果。
    await expect(cardStatus(settings)).toHaveCount(0);
    await expect(cardAlert(settings)).toHaveCount(0);

    // 另一张卡完全不受影响。
    await expect(saveButton(scope)).toHaveText(/^保存$/);
    await expect(saveButton(scope)).toBeEnabled();
    await expect(unassignedCheckbox(page)).toBeEnabled();
    await expect(chapterCheckbox(page, 'ch-1')).toBeEnabled();
    await expect(cardStatus(scope)).toHaveCount(0);
    await expect(cardAlert(scope)).toHaveCount(0);

    // 放行后端响应 → 落定成功态、控件恢复可用。
    api.controls.gates.settingsPut?.resolve();
    await expect(cardStatus(settings)).toContainText(SETTINGS_SAVED);
    await expect(newLimitInput(page)).toBeEnabled();
    await expect(saveButton(settings)).toHaveText(/^保存$/);
  });

  test('并发保存：两张卡各自落定自己的结果，互不串话', async ({ page }) => {
    const api = await openToday(page);
    const scope = scopeCard(page);
    const settings = settingsCard(page);

    api.controls.gates.scopePut = deferred<void>();
    api.controls.gates.settingsPut = deferred<void>();

    await saveButton(scope).click();
    await expect(saveButton(scope)).toHaveText(/^保存中…$/);
    // 范围卡保存中，设置卡仍可发起自己的保存。
    await saveButton(settings).click();
    await expect(saveButton(settings)).toHaveText(/^保存中…$/);
    await expect(unassignedCheckbox(page)).toBeDisabled();
    await expect(newLimitInput(page)).toBeDisabled();

    // 先放行设置：只有设置卡落定，范围卡仍在保存中、且不被波及。
    api.controls.gates.settingsPut?.resolve();
    await expect(cardStatus(settings)).toContainText(SETTINGS_SAVED);
    await expect(saveButton(scope)).toHaveText(/^保存中…$/);
    await expect(cardStatus(scope)).toHaveCount(0);
    await expect(cardAlert(scope)).toHaveCount(0);

    // 再放行范围：范围卡落定自己的成功消息（其后还会补一次 today 刷新）。
    api.controls.gates.scopePut?.resolve();
    await expect(cardStatus(scope)).toContainText(SCOPE_SAVED);

    // 消息各归各卡，互不串话。
    await expect(cardStatus(settings)).toContainText(SETTINGS_SAVED);
    await expect(cardStatus(settings)).not.toContainText(SCOPE_TITLE);
    await expect(cardStatus(scope)).not.toContainText(SETTINGS_SAVED);
    expect(api.count('PUT', '/api/learning/scope')).toBe(1);
    expect(api.count('PUT', '/api/learning/settings')).toBe(1);
  });

  test('范围保存成功但刷新失败：同卡警示 + 「重试刷新」只补发 GET today', async ({ page }) => {
    const api = await openToday(page);
    const scope = scopeCard(page);

    api.controls.failTodayGet = true;
    await saveButton(scope).click();

    const alert = cardAlert(scope);
    await expect(alert, '范围卡应出现「已保存但刷新失败」警示').toBeVisible();
    await expect(alert).toHaveAttribute('role', 'alert');
    await expect(alert).toContainText(SCOPE_SAVED);
    await expect(alert).toContainText('刷新失败');
    const retry = retryRefreshButton(scope);
    await expect(retry).toBeVisible();
    await expect(retry).toBeEnabled();

    expect(api.count('PUT', '/api/learning/scope')).toBe(1);
    // 初始 loadPage 一次 + 保存后失败的一次。
    expect(api.count('GET', '/api/learning/today')).toBe(2);

    // 后端恢复后点重试：只允许补发 GET /api/learning/today，绝不再发 PUT scope。
    api.controls.failTodayGet = false;
    const before = api.requests.length;
    await retry.click();
    await expect(cardStatus(scope), '重试成功后警示应清除为成功态').toContainText(SCOPE_SAVED);
    await expect(cardAlert(scope)).toHaveCount(0);

    const issued = api.requests.slice(before).map((r) => `${r.method} ${r.pathname}`);
    expect(issued, '重试刷新应只发出 GET /api/learning/today').toEqual(['GET /api/learning/today']);
    expect(api.count('PUT', '/api/learning/scope'), '不得有第二次 PUT /learning/scope').toBe(1);
    expect(api.count('PUT', '/api/learning/settings')).toBe(0);
  });

  test('保存成功后再编辑：成功消息消失，卡片回到未保存态', async ({ page }) => {
    const api = await openToday(page);
    const settings = settingsCard(page);

    await saveButton(settings).click();
    await expect(cardStatus(settings)).toContainText(SETTINGS_SAVED);

    await newLimitInput(page).fill('55');
    await expect(cardStatus(settings), '编辑后成功消息应立即消失').toHaveCount(0);
    await expect(cardAlert(settings)).toHaveCount(0);
    await expect(saveButton(settings)).toHaveText(/^保存$/);
    await expect(saveButton(settings)).toBeEnabled();
    // 编辑值不被回滚，可直接再次保存。
    await expect(newLimitInput(page)).toHaveValue('55');
    await saveButton(settings).click();
    await expect(cardStatus(settings)).toContainText(SETTINGS_SAVED);
    expect(api.count('PUT', '/api/learning/settings')).toBe(2);
  });

  test('视口截图（390/1280）与键盘可达性：Tab 可达保存与重试按钮、role 属性存在', async ({ page }) => {
    const api = await openToday(page);
    const scope = scopeCard(page);
    const settings = settingsCard(page);

    // —— 键盘：Tab 顺序可达两个保存按钮（DOM 顺序：范围卡在前，设置卡在后）——
    await tabToFocus(page, saveButton(scope));
    await tabToFocus(page, saveButton(settings));

    // —— 1280px：范围成功 + 设置失败 并存 ——
    api.controls.failSettingsPut = true;
    await newLimitInput(page).fill('33');
    await saveButton(settings).click();
    const settingsAlert = cardAlert(settings);
    await expect(settingsAlert).toBeVisible();
    await expect(settingsAlert).toHaveText(/\S+/);
    await saveButton(scope).click();
    await expect(cardStatus(scope)).toContainText(SCOPE_SAVED);
    // a11y 契约：role 是真属性而不是巧合文本。
    await expect(cardStatus(scope)).toHaveAttribute('role', 'status');
    await expect(settingsAlert).toHaveAttribute('role', 'alert');

    await page.screenshot({ path: shotPath('today-1280-mixed.png'), fullPage: true });
    await scope.screenshot({ path: shotPath('scope-card-1280-success.png') });
    await settings.screenshot({ path: shotPath('settings-card-1280-error.png') });

    // —— 390px：范围 warning（PUT 成功、刷新失败）+ 设置 error 并存 ——
    await page.setViewportSize({ width: 390, height: 844 });
    api.controls.failTodayGet = true;
    await saveButton(scope).click();
    const scopeAlert = cardAlert(scope);
    await expect(scopeAlert).toContainText(SCOPE_SAVED);
    await expect(scopeAlert).toContainText('刷新失败');
    await expect(scopeAlert).toHaveAttribute('role', 'alert');

    await page.screenshot({ path: shotPath('today-390-mixed.png'), fullPage: true });
    await scope.screenshot({ path: shotPath('scope-card-390-warning.png') });
    await settings.screenshot({ path: shotPath('settings-card-390-error.png') });

    // —— 键盘：警示内的「重试刷新」也可达（DOM 顺序在范围保存按钮之后）——
    await tabToFocus(page, retryRefreshButton(scope));

    // —— 补一张 390px 设置成功态（让两卡的成功/失败态都有截图留档）——
    api.controls.failSettingsPut = false;
    await saveButton(settings).click();
    await expect(cardStatus(settings)).toContainText(SETTINGS_SAVED);
    await settings.screenshot({ path: shotPath('settings-card-390-success.png') });
  });
});
