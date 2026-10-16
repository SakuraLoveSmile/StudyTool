import { expect, test, type Page } from '@playwright/test';
import { HOST_USER_A } from '../helpers/host-page';

const legacy = '资料中关于会计的定义有：(1) 美国会计学会（1966年）的定义：“确认、计量和传达经济信息的过程，以使信息使用者作出明智的判断和决策。”(2) 现代会计的定义：“会计是反映控制经济资源配置过程及其结果的信息系统。”资料还指出，会计以提供经济资源配置过程及结果的综合信息为主要手段。';
const citation = { chunk_id: 'source-1', doc_id: 'book-1', doc_name: '会计学概论书.docx', location: '段落 596-598', quote: '会计以提供经济信息为主要手段。' };

async function openSavedAnswer(page: Page, answer: string, stale = false) {
  const unexpected: string[] = [];
  await page.route(/^https?:\/\/[^/]+\/api\//, async route => {
    const path = new URL(route.request().url()).pathname;
    const responses: Record<string, unknown> = {
      '/api/auth/me': { user: HOST_USER_A, csrf_token: 'fixture-only', auth_required: true },
      '/api/materials': [{ id: 'book-1', filename: '会计学概论书.docx', file_type: 'textbook', status: 'completed' }],
      '/api/materials/tasks/history': { tasks: [], total: 0, page: 1, limit: 50, active_count: 0, actionable_count: 0, history_count: 0 },
      '/api/materials/tasks/active/list': [],
      '/api/library/chapters': [{ id: 'chapter-1', doc_id: 'book-1', title: '第一章 会计基本理论', chunk_count: 3, character_count: 800 }],
      '/api/library/guides': [],
      '/api/library/guides/tasks': [],
      '/api/library/answers': [{ id: 'answer-1', question: '会计的定义', answer, doc_ids: ['book-1'], chapter_id: 'chapter-1', previous_answer_id: null, citations: [citation, { ...citation, chunk_id: 'source-2', location: '段落 599-602' }], warnings: ['回答依据为所选范围内检索片段，并非全章总结。'], created_at: '2026-10-16T08:42:00Z', source_stale: stale }],
    };
    if (route.request().method() !== 'GET' || !(path in responses)) {
      unexpected.push(`${route.request().method()} ${path}`);
      return route.abort();
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(responses[path]) });
  });
  await page.goto('/library');
  await page.locator('.history-button', { hasText: '会计的定义' }).click();
  return unexpected;
}

test('旧回答行内编号分项阅读，引用可打开，无需再次生成', async ({ page }, info) => {
  await page.addInitScript(() => localStorage.setItem('studytool:theme', 'dark'));
  const unexpected = await openSavedAnswer(page, legacy);
  const article = page.getByRole('article', { name: '问答内容' });
  await expect(article.locator('.answer-text ol li')).toHaveCount(2);
  await expect(article.locator('.answer-text ol li').first()).toContainText('美国会计学会（1966年）');
  await expect(article.locator('.answer-text ol li').last()).toContainText('现代会计的定义');
  await expect(article).toContainText('并非全章总结');
  await expect(article.getByRole('region', { name: '回答依据' })).toContainText('2 处');
  await article.locator('.answer-citation').first().click();
  const source = page.getByRole('dialog', { name: '资料原文与定位溯源' });
  await expect(source).toContainText(citation.quote);
  await source.getByRole('button', { name: '关闭', exact: true }).last().click();
  await expect(source).toHaveCount(0);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: info.outputPath('answer-desktop-dark.png'), fullPage: true, animations: 'disabled' });
  expect(unexpected).toEqual([]);
});

test('标题列表加粗安全显示，普通括号和HTML原样保留；窄屏过期引用停用', async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const text = '## 两种定义\n\n1. **信息过程**：确认、计量和传达信息。\n2. **信息系统**：反映控制资源配置。\n\n### 阅读提示\n\n- 联系教材理解\n- 保留定义的适用范围\n\n> 以教材原文为依据。\n\n普通括号（1966年）不拆分。\n\n公式：(1) + (2) = 3 不拆分。\n\n<img src=x onerror=alert(1)>\n\n**';
  const unexpected = await openSavedAnswer(page, text, true);
  const article = page.getByRole('article', { name: '问答内容' });
  await expect(article.getByRole('heading', { name: '两种定义' })).toBeVisible();
  await expect(article.locator('.answer-text ol li')).toHaveCount(2);
  await expect(article.locator('.answer-text ul li')).toHaveCount(2);
  await expect(article.locator('.answer-text strong').first()).toHaveText('信息过程');
  await expect(article.locator('.answer-text blockquote')).toHaveText('以教材原文为依据。');
  await expect(article.locator('.answer-text')).toContainText('普通括号（1966年）不拆分。');
  await expect(article.locator('.answer-text')).toContainText('公式：(1) + (2) = 3 不拆分。');
  await expect(article.locator('.answer-text img')).toHaveCount(0);
  await expect(article.locator('.answer-text')).toContainText('<img src=x onerror=alert(1)>');
  await expect(article.locator('.answer-text p').last()).toHaveText('**');
  for (const button of await article.locator('.answer-citation').all()) await expect(button).toBeDisabled();
  expect(await page.locator('html').evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: info.outputPath('answer-mobile.png'), fullPage: true, animations: 'disabled' });
  expect(unexpected).toEqual([]);
});

test('行首括号公式保留原文，连续定义拆成独立列表项', async ({ page }) => {
  const unexpected = await openSavedAnswer(page, '(1) + (2) = 3\n\n(1) 第一种定义。(2) 第二种定义。');
  const content = page.getByRole('article', { name: '问答内容' }).locator('.answer-text');
  await expect(content.locator('p').first()).toHaveText('(1) + (2) = 3');
  await expect(content.locator('ol li')).toHaveText(['第一种定义。', '第二种定义。']);
  expect(unexpected).toEqual([]);
});
