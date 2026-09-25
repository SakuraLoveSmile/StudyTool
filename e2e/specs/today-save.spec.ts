import { expect, test, type Page, type Route } from '@playwright/test';

const send = (route: Route, status: number, body: unknown) =>
  route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

const scope = { chapter_ids: [] as string[], include_unassigned: true };
const settings = {
  daily_new_limit: 10, daily_review_limit: 10, reveal_after_seconds: null,
  auto_advance: false, show_hints: true, timezone: 'Asia/Shanghai',
};
const today = () => ({
  date: '2026-09-23', status: 'idle', total: 0, completed: 0, remaining: 0,
  new_count: 0, review_count: 0, attempts: [], queue: [], scope: { ...scope }, settings: { ...settings },
});

async function setup(page: Page, putBodies: Array<{ path: string; body: Record<string, unknown> }>) {
  await page.route(/\/api\/auth\/me(?:\?|$)/, route => send(route, 200, {
    user: { id: 'user-1', username: 'reviewer', must_change_password: false },
    csrf_token: 'csrf-test', auth_required: true,
  }));
  await page.route(/\/api\/materials\/tasks\/active\/list(?:\?|$)/, route => send(route, 200, []));
  await page.route(/\/api\/materials\/tasks\/history(?:\?|$)/, route => send(route, 200, {
    tasks: [], total: 0, page: 1, limit: 50, active_count: 0, actionable_count: 0, history_count: 0,
  }));
  await page.route(/\/api\/learning\/chapters(?:\?|$)/, route => send(route, 200, [
    { id: 'chapter-1', name: '第一章', sort_order: 0, enabled: true, card_count: 1 },
  ]));
  await page.route(/\/api\/learning\/settings(?:\?|$)/, route => {
    if (route.request().method() === 'PUT') {
      const body = route.request().postDataJSON();
      putBodies.push({ path: 'settings', body });
      Object.assign(settings, body);
    }
    return send(route, 200, settings);
  });
  await page.route(/\/api\/learning\/scope(?:\?|$)/, route => {
    if (route.request().method() === 'PUT') {
      const body = route.request().postDataJSON();
      putBodies.push({ path: 'scope', body });
      Object.assign(scope, body);
    }
    return send(route, 200, scope);
  });
  await page.route(/\/api\/learning\/today(?:\?|$)/, route => send(route, 200, today()));
  await page.goto('/today');
  await expect(page.getByRole('heading', { name: '今日设置' })).toBeVisible();
}

test('两张卡独立反馈，刷新不会抹掉另一张卡的未保存编辑', async ({ page }) => {
  const puts: Array<{ path: string; body: Record<string, unknown> }> = [];
  await setup(page, puts);
  const scopeCard = page.getByRole('heading', { name: '学习范围' }).locator('xpath=ancestor::section[1]');
  const settingsCard = page.getByRole('heading', { name: '今日设置' }).locator('xpath=ancestor::section[1]');
  await scopeCard.getByRole('checkbox', { name: /第一章/ }).check();
  await settingsCard.getByRole('spinbutton', { name: /每日新卡上限/ }).fill('23');
  await settingsCard.getByRole('button', { name: '保存' }).click();
  await expect(settingsCard.getByRole('status')).toContainText('今日设置已保存');
  await expect(scopeCard.getByRole('checkbox', { name: /第一章/ })).toBeChecked();
  expect(puts).toEqual([{ path: 'settings', body: expect.objectContaining({ daily_new_limit: 23 }) }]);
  await scopeCard.getByRole('button', { name: '保存' }).click();
  await expect(scopeCard.getByRole('status')).toContainText('学习范围已保存');
  expect(puts[1]).toEqual({ path: 'scope', body: expect.objectContaining({ chapter_ids: ['chapter-1'] }) });
});

test('PUT 成功但 GET 失败时只重试读取，不重复保存', async ({ page }) => {
  const puts: Array<{ path: string; body: Record<string, unknown> }> = [];
  await setup(page, puts);
  let failNextToday = true;
  await page.route(/\/api\/learning\/today(?:\?|$)/, route => {
    if (failNextToday) {
      failNextToday = false;
      return send(route, 503, { detail: '临时不可用' });
    }
    return send(route, 200, today());
  });
  const settingsCard = page.getByRole('heading', { name: '今日设置' }).locator('xpath=ancestor::section[1]');
  await settingsCard.getByRole('spinbutton', { name: /每日新卡上限/ }).fill('29');
  await settingsCard.getByRole('button', { name: '保存' }).click();
  await expect(settingsCard.getByRole('alert')).toContainText('已保存，但今日队列刷新失败');
  await settingsCard.getByRole('button', { name: '重试刷新' }).click();
  await expect(settingsCard.getByRole('status')).toContainText('今日设置已保存');
  expect(puts).toHaveLength(1);
  expect(puts[0].body.daily_new_limit).toBe(29);
});

test('迟到的今日队列读取不能覆盖稍后的编辑', async ({ page }) => {
  const puts: Array<{ path: string; body: Record<string, unknown> }> = [];
  await setup(page, puts);
  let release: (() => void) | undefined;
  const waiting = new Promise<void>(resolve => { release = resolve; });
  let started: (() => void) | undefined;
  const requested = new Promise<void>(resolve => { started = resolve; });
  await page.route(/\/api\/learning\/today(?:\?|$)/, async route => {
    started?.();
    await waiting;
    await send(route, 200, today());
  });
  await page.getByRole('button', { name: '刷新今日队列' }).click();
  await requested;
  const settingsCard = page.getByRole('heading', { name: '今日设置' }).locator('xpath=ancestor::section[1]');
  await settingsCard.getByRole('spinbutton', { name: /每日新卡上限/ }).fill('31');
  release?.();
  await expect(settingsCard.getByRole('spinbutton', { name: /每日新卡上限/ })).toHaveValue('31');
});

test('两卡在窄屏和明暗主题下可见、可聚焦且不横向溢出', async ({ page }) => {
  await setup(page, []);
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expect.poll(() => page.evaluate(() => document.documentElement.dataset.theme)).toBe(scheme);
    for (const width of [390, 640, 1280]) {
      await page.setViewportSize({ width, height: 800 });
      await expect(page.getByRole('heading', { name: '学习范围' })).toBeVisible();
      await expect(page.getByRole('heading', { name: '今日设置' })).toBeVisible();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      const button = page.getByRole('heading', { name: '今日设置' }).locator('xpath=ancestor::section[1]').getByRole('button', { name: '保存' });
      await button.focus();
      await expect(button).toBeFocused();
      await page.screenshot({ path: test.info().outputPath(`today-${scheme}-${width}.png`), fullPage: true });
    }
  }
});
