/**
 * 组件级 fixture 的极小静态服务器（**仅测试用途**）。
 *
 * 只服务两样东西：
 *  - `fixtures/`：组件 fixture 页
 *  - `@feedback/web` 的 dist 文件：来自 e2e/node_modules → 只读引用
 *    `frontend/vendor/feedback-web-0.2.0.tgz`，不读取也不修改 frontend/ 任何文件
 *
 * 端口 5276。默认只绑 127.0.0.1。
 */

import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { dirname, extname, join, normalize } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const fixturesDir = join(here, '..', 'fixtures');
const feedbackDist = join(here, '..', 'node_modules', '@feedback', 'web', 'dist');
const port = Number(process.env.FIXTURE_PORT ?? 5276);

/** @type {Record<string, string>} */
const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.cjs': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
};

function safeJoin(root, pathname) {
  const target = normalize(join(root, pathname));
  return target.startsWith(root) ? target : root;
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url ?? '/', 'http://127.0.0.1');
  const pathname = decodeURIComponent(url.pathname);

  // 组件产物：/vendor/* → e2e/node_modules/@feedback/web/dist/*
  const isVendor = pathname.startsWith('/vendor/');
  const root = isVendor ? feedbackDist : fixturesDir;
  const rel = isVendor ? pathname.slice('/vendor/'.length) : pathname.replace(/^\/+/, '') || 'widget-fixture.html';
  const file = safeJoin(root, rel);

  try {
    const body = await readFile(file);
    res.writeHead(200, { 'content-type': TYPES[extname(file)] ?? 'application/octet-stream' });
    res.end(body);
  } catch {
    res.writeHead(404, { 'content-type': 'text/plain; charset=utf-8' });
    res.end('not found');
  }
});

server.listen(port, '127.0.0.1', () => {
  console.log(`[fixture-server] http://127.0.0.1:${port}`);
});
