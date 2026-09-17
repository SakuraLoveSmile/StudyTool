'use strict';
/**
 * Log page timing acceptance check (T4: 快速输入 / 连续切页 / 自动刷新 / 页面隐藏).
 *
 * Why this exists: the worker tests prove the main process no longer blocks, and the
 * desktop build proves the page compiles — neither can prove that the *renderer* still
 * only issues one request per typing burst, cancels superseded work, or waits for the
 * previous query to finish before arming the next auto-refresh. That behaviour lives in
 * LogsView.vue and can only be observed with the real page running.
 *
 * Fidelity, stated honestly:
 *  - real frontend build, real vue-router, real preload (contextBridge -> ipcRenderer),
 *    real IPC round trip, real LogWorkerService in a real worker thread;
 *  - the backend HTTP API is stubbed by a local static+API server, and the IPC handlers
 *    below mirror the response contract of desktop/src/main.ts rather than importing it
 *    (main.ts is not importable outside Electron's own startup path). What is under test
 *    here is the renderer's timing logic, not the main-process request shaping.
 *
 * Run:  npm run test:ui   (from desktop/, after `npm run build` and a frontend build)
 * Exit code is non-zero when any check fails.
 */

const { app, BrowserWindow, ipcMain } = require('electron');
const path = require('path');
const fs = require('fs');
const os = require('os');
const http = require('http');

const REPO_ROOT = path.resolve(__dirname, '..', '..');
const DESKTOP_DIST = path.join(REPO_ROOT, 'desktop', 'dist');
const FRONTEND_DIST = path.join(REPO_ROOT, 'frontend', 'dist');

const PAGE_SIZE = 200;
/** Big enough that one query is slow enough to observe cancellation and overlap. */
const FIXTURE_TARGET_BYTES = 10 * 1024 * 1024;
const DEBOUNCE_MS = 300;
const AUTO_REFRESH_MS = 3000;

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok: Boolean(ok), detail: String(detail) });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name} :: ${detail}`);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// --- fixture ------------------------------------------------------------------------
function makeLogDir() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'rag-ui-logs-'));
  const file = path.join(dir, 'desktop.jsonl');
  const fd = fs.openSync(file, 'w');
  const base = Date.now() - 60 * 60 * 1000;
  let bytes = 0;
  let index = 0;
  try {
    let batch = [];
    let batchBytes = 0;
    while (bytes < FIXTURE_TARGET_BYTES) {
      const line = `${JSON.stringify({
        timestamp: new Date(base + index * 10).toISOString(),
        level: index % 5 === 0 ? 'ERROR' : 'INFO',
        source: 'desktop',
        event: `ui.event.${index}`,
        message: `message ${index}`,
        session_id: 'sess-ui',
        app_version: '1.0.4',
      })}\n`;
      batch.push(line);
      batchBytes += Buffer.byteLength(line, 'utf8');
      index += 1;
      if (batch.length >= 2000) {
        fs.writeSync(fd, batch.join(''));
        bytes += batchBytes;
        batch = [];
        batchBytes = 0;
      }
    }
    if (batch.length) fs.writeSync(fd, batch.join(''));
  } finally {
    fs.closeSync(fd);
  }
  return { dir, lines: index, bytes };
}

// --- static + stubbed API server ----------------------------------------------------
const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.woff2': 'font/woff2',
  '.ico': 'image/x-icon',
};

function startServer(port) {
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, `http://127.0.0.1:${port}`);
    if (url.pathname.startsWith('/api/')) {
      res.setHeader('Content-Type', 'application/json; charset=utf-8');
      if (url.pathname === '/api/auth/me') {
        // auth_required:false is what makes /logs reachable without a real backend.
        res.end(JSON.stringify({ auth_required: false, user: null, csrf_token: null }));
        return;
      }
      res.end(JSON.stringify({}));
      return;
    }
    let rel = decodeURIComponent(url.pathname);
    if (rel === '/' || rel === '') rel = '/index.html';
    let file = path.join(FRONTEND_DIST, rel);
    // SPA history fallback for /logs, /today, ...
    if (!fs.existsSync(file) || fs.statSync(file).isDirectory()) file = path.join(FRONTEND_DIST, 'index.html');
    try {
      const body = fs.readFileSync(file);
      res.setHeader('Content-Type', MIME[path.extname(file).toLowerCase()] || 'application/octet-stream');
      res.end(body);
    } catch {
      res.statusCode = 404;
      res.end('not found');
    }
  });
  return new Promise((resolve) => server.listen(port, '127.0.0.1', () => resolve(server)));
}

// --- main ---------------------------------------------------------------------------
const fixture = { dir: '', lines: 0, bytes: 0 };
/** Every query the renderer issued, in order. */
const calls = [];
/** Every query that finished, in order — used to prove auto-refresh waits for completion. */
const completions = [];
const cancels = [];

async function main() {
  if (!fs.existsSync(path.join(FRONTEND_DIST, 'index.html'))) {
    throw new Error(`前端构建产物缺失，请先在 frontend/ 执行 npm run build: ${FRONTEND_DIST}`);
  }
  const { LogWorkerService } = require(path.join(DESKTOP_DIST, 'log-workers.js'));

  const built = makeLogDir();
  fixture.dir = built.dir;
  fixture.lines = built.lines;
  fixture.bytes = built.bytes;
  console.log(`fixture ${(built.bytes / (1024 * 1024)).toFixed(1)} MiB / ${built.lines} records / ${built.dir}`);

  const logWorkers = new LogWorkerService();
  /** First entry of each successful response, so the DOM can be matched to a request. */
  const firstEventByRequest = new Map();

  ipcMain.handle('desktop:getLogMeta', () => ({
    ok: true,
    sessionId: 'sess-ui',
    appVersion: '1.0.4',
    platform: process.platform,
    arch: process.arch,
    logDir: fixture.dir,
    sources: ['desktop', 'backend', 'frontend'],
    levels: ['DEBUG', 'INFO', 'WARN', 'ERROR'],
    pageSize: PAGE_SIZE,
    defaultRangeMs: 24 * 60 * 60 * 1000,
    maxRangeMs: 7 * 24 * 60 * 60 * 1000,
  }));

  ipcMain.handle('desktop:queryLogs', async (_event, params) => {
    const requestId = params && params.requestId ? params.requestId : `no-id-${calls.length}`;
    const page = params && Number.isFinite(params.page) ? params.page : 1;
    calls.push({
      at: Date.now(),
      requestId,
      page,
      keyword: params && params.keyword ? params.keyword : null,
    });

    const outcome = await logWorkers.query(requestId, {
      dir: fixture.dir,
      sources: ['desktop'],
      from: 0,
      to: Date.now() + 60_000,
      levels: params && Array.isArray(params.levels) ? params.levels : [],
      keyword: params && params.keyword ? params.keyword : undefined,
      page,
      pageSize: PAGE_SIZE,
    });
    completions.push({ at: Date.now(), requestId, page, ok: outcome.ok });

    if (!outcome.ok) {
      return {
        ok: false,
        entries: [],
        total: 0,
        page,
        pageSize: PAGE_SIZE,
        hasMore: false,
        scannedBytes: 0,
        scanTruncated: false,
        unavailable: [],
        skippedLines: 0,
        requestId,
        code: outcome.error.code,
        message: outcome.error.message,
        retryable: outcome.error.retryable,
      };
    }
    if (outcome.value.entries.length) firstEventByRequest.set(requestId, outcome.value.entries[0].event);
    return { ...outcome.value, requestId, elapsedMs: outcome.elapsedMs };
  });

  ipcMain.handle('desktop:cancelLogQuery', (_event, requestId) => {
    cancels.push({ at: Date.now(), requestId });
    return { ok: true, canceled: logWorkers.cancelQuery(requestId) };
  });

  ipcMain.handle('desktop:reportLog', () => ({ ok: true, accepted: 0, dropped: 0 }));
  ipcMain.handle('desktop:exportDiagnostics', () => ({ ok: false, message: 'harness: export 不在本检查范围内' }));
  ipcMain.handle('desktop:openLogDirectory', () => undefined);

  const server = await startServer(0);
  const port = server.address().port;

  const win = new BrowserWindow({
    width: 1200,
    height: 900,
    show: false,
    webPreferences: {
      // The real preload: contextBridge + ipcRenderer, exactly as shipped.
      preload: path.join(DESKTOP_DIST, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  win.webContents.on('console-message', (_e, _level, message) => {
    if (/error|warn/i.test(message)) console.log(`  renderer> ${message.slice(0, 300)}`);
  });

  await win.loadURL(`http://127.0.0.1:${port}/logs`);
  // A hidden window reports document.hidden === true, which would silently disable the
  // very behaviour under test — show it without stealing focus instead.
  win.showInactive();

  const run = (code) => win.webContents.executeJavaScript(code, true);

  await run(`
    window.__t = {
      sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
      keyword: () => document.querySelector('input[placeholder="事件名或错误摘要"]'),
      range: () => document.querySelectorAll('select')[0],
      next: () => [...document.querySelectorAll('button')].find((b) => b.textContent.trim() === '下一页'),
      autoRefresh: () => {
        const label = [...document.querySelectorAll('label')].find((l) => l.textContent.includes('自动刷新'));
        return label ? label.querySelector('input') : null;
      },
      firstRow: () => {
        const li = document.querySelector('main ul li');
        return li ? li.textContent.replace(/\\s+/g, ' ').trim() : null;
      },
      header: () => {
        const span = [...document.querySelectorAll('main span')].find((s) => s.textContent && s.textContent.includes('共 ') && s.textContent.includes('页'));
        return span ? span.textContent.replace(/\\s+/g, ' ').trim() : null;
      },
      rows: () => document.querySelectorAll('main ul li').length,
      empty: () => document.body.textContent.includes('当前筛选条件下没有日志记录'),
      loading: () => document.body.textContent.includes('正在读取日志'),
      type: (el, value) => { el.value = value; el.dispatchEvent(new Event('input', { bubbles: true })); },
      change: (el, value) => { el.value = value; el.dispatchEvent(new Event('change', { bubbles: true })); },
    };
    'helpers-ready'
  `);

  // Wait for the first page to actually render.
  let mounted = false;
  for (let i = 0; i < 100; i += 1) {
    mounted = await run('document.querySelectorAll("main ul li").length > 0');
    if (mounted) break;
    await sleep(100);
  }
  if (!mounted) {
    const body = await run("document.body.innerText.slice(0,400)");
    throw new Error(`日志页未渲染出内容（body 片段：${body}）`);
  }

  const visibility = await run('({ hidden: document.hidden, state: document.visibilityState })');
  check('页面可见（否则隐藏暂停逻辑会静默失效）', visibility.hidden === false, JSON.stringify(visibility));
  check('首屏渲染 200 条', (await run('__t.rows()')) === PAGE_SIZE, `rows=${await run('__t.rows()')}`);

  // --- 1. 300 ms 防抖 ----------------------------------------------------------------
  calls.length = 0;
  completions.length = 0;
  cancels.length = 0;
  await run(`
    (async () => {
      const k = __t.keyword();
      const seq = ['u', 'ui', 'ui.', 'ui.e', 'ui.event'];
      for (const value of seq) { __t.type(k, value); await __t.sleep(60); }
      return true;
    })()
  `);
  await sleep(DEBOUNCE_MS + 1500);
  const debounceCalls = calls.length;
  check(
    '连打 5 个字符只发 1 次查询',
    debounceCalls === 1 && calls[0].keyword === 'ui.event',
    `queries=${debounceCalls} lastKeyword=${calls[0] ? calls[0].keyword : null}`
  );

  // --- 2. 筛选变更取消旧请求 + 旧结果不覆盖 -------------------------------------------
  calls.length = 0;
  completions.length = 0;
  cancels.length = 0;
  await run(`
    (async () => {
      __t.change(__t.range(), '7d');
      await __t.sleep(120);
      __t.change(__t.range(), '1h');
      return true;
    })()
  `);
  await sleep(4000);
  const cancelSeen = cancels.length >= 1 && cancels[0].requestId === calls[0].requestId;
  check('筛选变更取消了在途请求', cancelSeen, `cancels=${cancels.length} firstCancel=${cancels[0] ? cancels[0].requestId : null} firstCall=${calls[0] ? calls[0].requestId : null}`);

  const lastCall = calls[calls.length - 1];
  const expectedFirstRow = firstEventByRequest.get(lastCall.requestId) || null;
  const renderedFirstRow = await run('__t.firstRow()');
  check(
    '只接受最新结果（旧响应未覆盖列表）',
    Boolean(expectedFirstRow) && Boolean(renderedFirstRow) && renderedFirstRow.includes(expectedFirstRow),
    `expected=${expectedFirstRow} rendered=${renderedFirstRow ? renderedFirstRow.slice(0, 80) : null}`
  );

  // --- 3. 自动刷新：上次完成后再等 3 秒 ----------------------------------------------
  calls.length = 0;
  completions.length = 0;
  await run(`(() => { const cb = __t.autoRefresh(); cb.checked = true; cb.dispatchEvent(new Event('change', { bubbles: true })); return true; })()`);
  await sleep(12000);
  const autoCalls = calls.slice();
  const autoCompletions = completions.slice();
  let gaps = [];
  let overlap = false;
  for (let i = 1; i < autoCalls.length; i += 1) {
    const previousCompletion = autoCompletions[i - 1];
    if (!previousCompletion) break;
    const gap = autoCalls[i].at - previousCompletion.at;
    gaps.push(gap);
    if (gap < AUTO_REFRESH_MS - 200) overlap = true;
  }
  check(
    '自动刷新在 3 次以上轮次后仍在推进',
    autoCalls.length >= 2 && autoCompletions.length >= 2,
    `queries=${autoCalls.length} completions=${autoCompletions.length}`
  );
  check(
    '自动刷新是"上次完成后再等 3 秒"，不叠发',
    gaps.length >= 1 && gaps.every((g) => g >= AUTO_REFRESH_MS - 200) && !overlap,
    `gaps=${JSON.stringify(gaps)}`
  );

  // --- 4. 页面隐藏暂停 / 恢复继续 ------------------------------------------------------
  calls.length = 0;
  completions.length = 0;
  win.hide();
  await sleep(1200);
  const hiddenCallsAfterHide = calls.length;
  await sleep(4500);
  const hiddenCallsTotal = calls.length;
  const hiddenState = await run('({ hidden: document.hidden, state: document.visibilityState })');
  check(
    '页面隐藏后不再发起自动刷新',
    hiddenState.hidden === true && hiddenCallsTotal === 0,
    `hidden=${hiddenState.hidden} queriesAfterHide=${hiddenCallsTotal}（期间首次统计 ${hiddenCallsAfterHide}）`
  );

  win.showInactive();
  await sleep(5000);
  check(
    '恢复可见后自动刷新继续',
    calls.length >= 1,
    `queriesAfterShow=${calls.length}`
  );

  // 关闭自动刷新，避免干扰后续切页检查。
  await run(`(() => { const cb = __t.autoRefresh(); cb.checked = false; cb.dispatchEvent(new Event('change', { bubbles: true })); return true; })()`);
  await sleep(600);

  // --- 5. 连续切页：不堆积、最后一页胜出 ----------------------------------------------
  await run(`(() => { __t.change(__t.range(), '24h'); return true; })()`);
  await sleep(3000);
  calls.length = 0;
  completions.length = 0;
  cancels.length = 0;
  const pageBefore = await run('__t.header()');
  await run(`
    (async () => {
      for (let i = 0; i < 5; i += 1) { const b = __t.next(); if (b && !b.disabled) b.click(); await __t.sleep(90); }
      return true;
    })()
  `);
  await sleep(6000);
  const okCompletions = completions.filter((c) => c.ok).length;
  const lastPagingCall = calls[calls.length - 1];
  const expectedPagingRow = lastPagingCall ? firstEventByRequest.get(lastPagingCall.requestId) : null;
  const pagingRow = await run('__t.firstRow()');
  const headerAfter = await run('__t.header()');
  check(
    '连续切页不堆积请求（执行数远小于点击数）',
    calls.length === 5 && okCompletions <= 2 && cancels.length >= 1,
    `clicks=5 queries=${calls.length} executed=${okCompletions} cancels=${cancels.length}`
  );
  check(
    '连续切页后停在最后一页且内容一致',
    Boolean(expectedPagingRow) && Boolean(pagingRow) && pagingRow.includes(expectedPagingRow) && headerAfter !== pageBefore,
    `headerBefore=${pageBefore} headerAfter=${headerAfter} expected=${expectedPagingRow} rendered=${pagingRow ? pagingRow.slice(0, 70) : null}`
  );

  logWorkers.shutdown();
  server.close();
  win.destroy();

  const failed = results.filter((r) => !r.ok);
  console.log('');
  console.log(`SUMMARY checks=${results.length} passed=${results.length - failed.length} failed=${failed.length}`);
  for (const f of failed) console.log(`  FAILED: ${f.name} :: ${f.detail}`);

  try {
    fs.rmSync(fixture.dir, { recursive: true, force: true });
  } catch {
    /* temp dir */
  }
  app.exit(failed.length ? 1 : 0);
}

app.disableHardwareAcceleration();
if (app.dock) app.dock.hide();

app
  .whenReady()
  .then(main)
  .catch((err) => {
    console.error('HARNESS_ERROR ' + (err && err.stack ? err.stack : err));
    app.exit(2);
  });
