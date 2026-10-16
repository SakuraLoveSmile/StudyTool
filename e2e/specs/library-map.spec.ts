import { expect, test, type Page } from '@playwright/test';
import { HOST_USER_A } from '../helpers/host-page';

// Saved-guide fixtures only. All API requests are intercepted; no model or source data is changed.
const citation = { chunk_id: 'source-1', doc_id: 'book-1', doc_name: '会计学.docx', location: '段落 3', quote: '会计的产生源于经济管理的需要。' };
const concept = (name: string, explanation: string) => ({ name, explanation, recitation: `${name}的背诵短段。`, keywords: ['经济管理'], citations: [citation] });
const guide = {
  id: 'guide-old', doc_id: 'book-1', chapter_id: 'chapter-1', chapter_title: '第一章 会计基本理论',
  summary: '本章介绍会计的产生、沿革和基本职能。',
  concepts: [concept('会计的产生', '会计随着经济活动发展和管理需要而产生。'), concept('会计 核算', '会计核算以货币为主要计量单位。')],
  mind_map: { title: '第一章 会计基本理论', children: [
    { title: '会计的产生及发展', children: [{ title: '会计的产生' }, { title: '会计的沿革与发展' }] },
    { title: '会计职能', children: [{ title: '会计核算' }] },
    // A duplicate title in another position must not make the missing leaf look expandable.
    { title: '会计的沿革与发展', children: [{ title: '会计的产生' }] },
  ] },
  connections: [], exam_points: [], citations: [citation], warnings: [], created_at: '2026-10-16T09:00:00Z', source_stale: false,
};

async function openMap(page: Page, stale = false) {
  const unexpected: string[] = [];
  await page.route(/^https?:\/\/[^/]+\/api\//, async route => {
    const url = new URL(route.request().url());
    const responses: Record<string, unknown> = {
      '/api/auth/me': { user: HOST_USER_A, csrf_token: 'fixture-only', auth_required: true },
      '/api/materials': [{ id: 'book-1', filename: '会计学.docx', file_type: 'textbook', status: 'completed' }],
      '/api/materials/tasks/history': { tasks: [], total: 0, page: 1, limit: 50, active_count: 0, actionable_count: 0, history_count: 0 },
      '/api/materials/tasks/active/list': [],
      '/api/library/chapters': [{ id: 'chapter-1', doc_id: 'book-1', title: '第一章 会计基本理论', chunk_count: 3, character_count: 800 }],
      '/api/library/guides': [{ ...guide, source_stale: stale }],
      '/api/library/guides/tasks': [],
      '/api/library/answers': [],
    };
    if (route.request().method() !== 'GET' || !(url.pathname in responses)) {
      unexpected.push(`${route.request().method()} ${url.pathname}`);
      return route.abort();
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(responses[url.pathname]) });
  });
  await page.goto('/library');
  await page.getByRole('tab', { name: '思维导图', exact: true }).click();
  return unexpected;
}

test('末级概念展示解读和原文，分组展示真实子节点，旧缺失叶子明确提示', async ({ page }, info) => {
  const unexpected = await openMap(page);
  const map = page.getByRole('region', { name: '章节思维导图' });
  const detail = page.getByRole('complementary', { name: '知识点详情' });
  await map.locator('[data-node-path="b0"] .map-node').click();
  await expect(detail).toContainText('下级知识点');
  await detail.getByRole('button', { name: '会计的产生', exact: true }).click();
  await expect(detail).toContainText('会计随着经济活动发展和管理需要而产生。');
  await detail.getByText('背诵短段', { exact: true }).click();
  await expect(detail).toContainText('会计的产生的背诵短段。');
  await detail.getByRole('button', { name: '查看原文 1' }).click();
  const source = page.getByRole('dialog', { name: '资料原文与定位溯源' });
  await expect(source).toContainText(citation.quote);
  await source.getByRole('button', { name: '关闭', exact: true }).last().click();
  await expect(source).toHaveCount(0);
  await map.locator('[data-node-path="b0-l1"] .map-node').click();
  await expect(detail).toContainText('未提供此末级知识点的独立解读');
  await expect(detail).toContainText('当前没有更多分支');
  await expect(detail.getByRole('heading', { name: '下级知识点' })).toHaveCount(0);
  await expect(detail.getByRole('button', { name: /查看原文/ })).toHaveCount(0);
  await expect(page.getByText('此节点为章节结构主题。', { exact: false })).toHaveCount(0);
  await map.getByRole('button', { name: '全屏查看' }).click();
  await page.screenshot({ path: info.outputPath('missing-leaf.png'), animations: 'disabled' });
  await page.getByRole('button', { name: '查看章节总结' }).click();
  await expect(page.getByRole('tab', { name: '章节总结', exact: true })).toHaveAttribute('aria-selected', 'true');
  expect(await page.locator('body').evaluate(el => el.style.overflow)).not.toBe('hidden');
  expect(unexpected).toEqual([]);
});

test('章节节点展示概览，空格差异可匹配；窄屏和过期引用保持正确', async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => localStorage.setItem('studytool:theme', 'dark'));
  const unexpected = await openMap(page, true);
  const map = page.getByRole('region', { name: '章节思维导图' });
  const detail = page.getByRole('complementary', { name: '知识点详情' });
  await map.locator('[data-node-path="root"] .map-node').click();
  await expect(detail).toContainText(guide.summary);
  await expect(detail).toContainText('章节主题');
  await detail.getByRole('button', { name: '会计职能', exact: true }).click();
  await detail.getByRole('button', { name: '会计核算', exact: true }).click();
  await expect(map.locator('[data-node-path="b1-l0"] .map-node')).toHaveAttribute('aria-pressed', 'true');
  await expect(detail).toContainText('会计核算以货币为主要计量单位。');
  await expect(detail.getByRole('button', { name: '查看原文 1' })).toBeDisabled();
  await expect(detail).toContainText('来源尚未核实或已变化');
  expect(await page.locator('html').evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath('mobile-detail.png'), animations: 'disabled' });
  expect(unexpected).toEqual([]);
});
