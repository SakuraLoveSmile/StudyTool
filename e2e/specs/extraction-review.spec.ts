import { expect, test, type Page, type Route } from '@playwright/test';

const json = (route: Route, status: number, body: unknown) =>
  route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

const batch = {
  id: 'batch-resume', task_id: null, status: 'completed', task_status: 'completed',
  doc_ids: ['doc-1'], candidates_count: 1, pending_candidates: 1,
  created_at: '2026-09-23T01:00:00Z', updated_at: '2026-09-23T01:00:00Z',
};

const candidate = {
  id: 'candidate-1', extraction_id: batch.id, name: '组织结构', chapter: '第一章',
  definition: '组织结构的基本概念', citations: [], status: 'pending', match_type: 'new',
  version: 7, requires_source_review: false, created_at: '2026-09-23T01:00:00Z',
};

async function mockAnalysisPage(page: Page, applyBodies: string[], requiresSourceReview = false) {
  await page.route(/\/api\/auth\/me(?:\?|$)/, (route) => json(route, 200, {
    user: { id: 'user-1', username: 'reviewer', must_change_password: false }, csrf_token: 'csrf-test', auth_required: true,
  }));
  await page.route(/\/api\/materials(?:\?|$)/, (route) => json(route, 200, [{
    id: 'doc-1', filename: '教材.pdf', file_hash: 'hash-1', file_type: 'textbook', status: 'completed',
    error_msg: null, page_count: 2, year: null, session_name: null, created_at: '2026-09-23T00:00:00Z',
  }]));
  await page.route(/\/api\/analysis\/(stats|point-stats|knowledge-points\/legacy)(?:\?|$)/, (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/stats')) return json(route, 200, {
      total_recorded_years: 0, min_year: null, max_year: null, recorded_years: [], missing_years: [],
      recent_5yr_window: [], total_questions: 0, confirmed_questions: 0, knowledge_points_count: 0,
    });
    return json(route, 200, []);
  });
  await page.route(/\/api\/materials\/tasks\/active\/list(?:\?|$)/, (route) => json(route, 200, []));
  await page.route(/\/api\/materials\/tasks\/history(?:\?|$)/, (route) => json(route, 200, { tasks: [], total: 0, page: 1, limit: 50, active_count: 0, actionable_count: 0, history_count: 0 }));
  await page.route(/\/api\/analysis\/knowledge-points(?:\?|$)/, (route) => json(route, 200, []));
  await page.route(/\/api\/analysis\/extractions\/latest(?:\?|$)/, (route) => json(route, 200, batch));
  await page.route(/\/api\/analysis\/extractions\?(?:.*)$/, (route) => json(route, 200, { items: [batch], total: 1 }));
  await page.route(/\/api\/analysis\/extractions\/batch-resume\/candidates(?:\?|$)/, (route) => json(route, 200, [
    requiresSourceReview ? {
      ...candidate,
      requires_source_review: true,
      reason: '来源已变化',
      citations: [{ chunk_id: 'chunk-1', doc_id: 'doc-1', doc_name: '教材.pdf', page_number: 1, paragraph_ref: null, quote: '原文依据' }],
    } : candidate,
  ]));
  await page.route(/\/api\/analysis\/extractions\/batch-resume\/question-links(?:\?|$)/, (route) => json(route, 200, { links: [], total_confirmed_questions: 0 }));
  await page.route(/\/api\/analysis\/extractions\/batch-resume\/apply(?:\?|$)/, async (route) => {
    applyBodies.push(route.request().postData() || '');
    await json(route, 200, { applied_count: 1, ignored_count: 0, merged_count: 0, replayed_count: 0, results: [] });
  });
  await page.goto('/analysis');
  await expect(page.getByRole('button', { name: '从资料提取考点' }).first()).toBeVisible();
}

test('恢复批次只读加载，且不会因恢复流程创建新提取任务', async ({ page }) => {
  const applyBodies: string[] = [];
  let createCount = 0;
  await mockAnalysisPage(page, applyBodies);
  await page.route(/\/api\/analysis\/extractions$/, (route) => {
    if (route.request().method() === 'POST') createCount += 1;
    return json(route, 500, { detail: '恢复测试不应创建新任务' });
  });
  await page.getByRole('button', { name: '从资料提取考点' }).first().click();
  await expect(page.getByText('可恢复的提取批次')).toBeVisible();
  await page.getByRole('button', { name: /继续核对/ }).click();
  await expect(page.getByText('组织结构', { exact: true })).toBeVisible();
  expect(createCount).toBe(0);
  await page.getByRole('button', { name: '确认并更新考点目录' }).click();
  await expect.poll(() => applyBodies.length).toBe(1);
  expect(JSON.parse(applyBodies[0]).decisions[0]).toMatchObject({ candidate_id: candidate.id, version: 7 });
});

test('来源核对 409 保留核对界面并可重试', async ({ page }) => {
  const applyBodies: string[] = [];
  await mockAnalysisPage(page, applyBodies, true);
  let sourceGets = 0;
  await page.route(/\/api\/analysis\/extractions\/batch-resume\/candidates\/candidate-1\/source-review(?:\?|$)/, async (route) => {
    if (route.request().method() === 'GET') {
      sourceGets += 1;
      if (sourceGets === 1) return json(route, 409, { detail: '来源已变化，请重新加载' });
      return json(route, 200, { candidate_id: candidate.id, version: 8, name: candidate.name, chapter: candidate.chapter, citations: [], can_confirm: false, reason: '当前来源不可确认' });
    }
    return json(route, 409, { detail: '版本已过期' });
  });
  await page.getByRole('button', { name: '从资料提取考点' }).first().click();
  await page.getByRole('button', { name: /继续核对/ }).click();
  await expect(page.getByText('组织结构', { exact: true })).toBeVisible();
  await page.getByText(/查看看出处依据/).click();
  await page.getByRole('button', { name: '核对依据' }).click();
  await expect(page.getByText('来源已变化，请重新加载')).toBeVisible();
  await page.getByRole('button', { name: '重新加载当前原文' }).click();
  await expect(page.getByText('当前来源不可确认')).toBeVisible();
  expect(sourceGets).toBe(2);
});

test('来源核对成功后使用新候选版本审核', async ({ page }) => {
  const applyBodies: string[] = [];
  await mockAnalysisPage(page, applyBodies, true);
  let reviewBody: Record<string, unknown> | null = null;
  const citation = {
    chunk_id: 'chunk-1', doc_id: 'doc-1', doc_name: '教材.pdf',
    page_number: 1, paragraph_ref: null, quote: '当前原文依据',
    source_hash: 'current-hash', content: '当前原文依据已核实',
  };
  await page.route(/\/api\/analysis\/extractions\/batch-resume\/candidates\/candidate-1\/source-review(?:\?|$)/, route => {
    if (route.request().method() === 'GET') return json(route, 200, {
      candidate_id: candidate.id, version: 7, name: candidate.name, chapter: candidate.chapter,
      citations: [citation], can_confirm: true, reason: '旧依据已变化，请核对当前原文',
    });
    reviewBody = route.request().postDataJSON();
    return json(route, 200, {
      ...candidate, version: 8, source_status: 'valid', requires_source_review: false,
      citations: [citation],
    });
  });
  await page.getByRole('button', { name: '从资料提取考点' }).first().click();
  await page.getByRole('button', { name: /继续核对/ }).click();
  await page.getByText(/查看看出处依据/).click();
  await page.getByRole('button', { name: '核对依据' }).click();
  const group = page.getByRole('group', { name: /核对 组织结构 的来源/ });
  await expect(group.getByText('当前原文依据已核实')).toBeVisible();
  await group.getByRole('checkbox').check();
  await group.getByRole('button', { name: '确认当前依据' }).click();
  await expect(group).toHaveCount(0);
  expect(reviewBody).toMatchObject({ version: 7, citations: [expect.objectContaining({ source_hash: 'current-hash' })] });
  await page.getByRole('button', { name: '确认并更新考点目录' }).click();
  await expect.poll(() => applyBodies.length).toBe(1);
  expect(JSON.parse(applyBodies[0]).decisions[0].version).toBe(8);
});

test('恢复审核在明暗主题和窄屏下可读、可聚焦', async ({ page }) => {
  await mockAnalysisPage(page, []);
  await page.getByRole('button', { name: '从资料提取考点' }).first().click();
  await page.getByRole('button', { name: /继续核对/ }).click();
  await expect(page.getByText('组织结构', { exact: true })).toBeVisible();
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expect.poll(() => page.evaluate(() => document.documentElement.dataset.theme)).toBe(scheme);
    for (const width of [390, 640, 1280]) {
      await page.setViewportSize({ width, height: 800 });
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      const confirm = page.getByRole('button', { name: '确认并更新考点目录' });
      await confirm.focus();
      await expect(confirm).toBeFocused();
      if (width === 390) {
        const box = await confirm.boundingBox();
        expect(box).not.toBeNull();
        const hit = await page.evaluate(({ x, y }) => {
          const target = document.elementFromPoint(x, y);
          return target?.closest('button')?.textContent?.includes('确认并更新考点目录') ?? false;
        }, { x: box!.x + box!.width - 8, y: box!.y + box!.height / 2 });
        expect(hit).toBe(true);
      }
      await page.screenshot({ path: test.info().outputPath(`extraction-${scheme}-${width}.png`), fullPage: true });
    }
  }
});

test('旧版 invalid 候选只读展示，不会发送空审核', async ({ page }) => {
  const applyBodies: string[] = [];
  await mockAnalysisPage(page, applyBodies);
  await page.route(/\/api\/analysis\/extractions\/batch-resume\/candidates(?:\?|$)/, route => json(route, 200, [
    { ...candidate, status: 'invalid', source_status: 'invalid', requires_source_review: true },
  ]));
  await page.getByRole('button', { name: '从资料提取考点' }).first().click();
  await page.getByRole('button', { name: /继续核对/ }).click();
  await expect(page.getByText(/原审核决定无法确认，已保持只读/)).toBeVisible();
  await expect(page.getByRole('button', { name: '确认并更新考点目录' })).toBeDisabled();
  expect(applyBodies).toHaveLength(0);
});
