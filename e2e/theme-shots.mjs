// 深色模式可视化验收：双主题 × 页面 × 宽度截图 + 对比度抽样。
// 用法：node theme-shots.mjs（需 dev server 跑在 5174）
import { chromium } from '@playwright/test';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';

const BASE = 'http://127.0.0.1:5174';
const OUT = fileURLToPath(new URL('./theme-shots/', import.meta.url));
fs.mkdirSync(OUT, { recursive: true });

const PAGES = [
  ['today', '/today'],
  ['recall', '/recall'],
  ['materials', '/materials'],
  ['analysis', '/analysis'],
  ['cards', '/cards'],
  ['practice', '/practice'],
  ['settings', '/settings'],
  ['login', '/login'],
];
const WIDTHS = [390, 768, 1024, 1280];

const browser = await chromium.launch();
const report = [];

for (const width of WIDTHS) {
  for (const theme of ['light', 'dark']) {
    const page = await browser.newPage({ viewport: { width, height: 860 } });
    await page.emulateMedia({ colorScheme: theme });
    for (const [name, path] of PAGES) {
      await page.goto(BASE + path, { waitUntil: 'networkidle' }).catch(() => {});
      await page.waitForTimeout(400);
      const actual = await page.evaluate(() => document.documentElement.dataset.theme);
      const scrollW = await page.evaluate(() => document.documentElement.scrollWidth);
      const overflow = scrollW > width ? ` OVERFLOW(${scrollW}px)` : '';
      const file = `${OUT}${name}-${width}-${theme}.png`;
      await page.screenshot({ path: file, fullPage: false });
      report.push(`${name.padEnd(10)} ${width}px ${theme}: theme=${actual}${overflow}`);
    }
    await page.close();
  }
}

// 对比度抽样：关键文字角色在深色下的实测对比度
const page = await browser.newPage({ viewport: { width: 1280, height: 860 } });
await page.goto(BASE + '/today', { waitUntil: 'networkidle' }).catch(() => {});
await page.waitForTimeout(500);
for (const theme of ['light', 'dark']) {
  await page.evaluate((t) => {
    document.documentElement.dataset.theme = t;
    document.documentElement.style.colorScheme = t;
  }, theme);
  await page.waitForTimeout(150);
  const ratios = await page.evaluate(() => {
    const lum = (rgb) => {
      const [r, g, b] = rgb.match(/\d+/g).map(Number).map((v) => {
        v /= 255;
        return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
      });
      return 0.2126 * r + 0.7152 * g + 0.0722 * b;
    };
    const parse = (rgb) => rgb.match(/[\d.]+/g).map(Number);
    const effBg = (el) => {
      let node = el;
      while (node && node !== document.documentElement) {
        const bg = getComputedStyle(node).backgroundColor;
        const a = parse(bg)[3];
        if (a !== undefined && a < 1) { node = node.parentElement; continue; }
        if (bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') return bg;
        node = node.parentElement;
      }
      return getComputedStyle(document.body).backgroundColor;
    };
    const ratio = (fg, bg) => {
      const l1 = Math.max(lum(fg), lum(bg));
      const l2 = Math.min(lum(fg), lum(bg));
      return ((l1 + 0.05) / (l2 + 0.05)).toFixed(2);
    };
    const grab = (sel) => {
      const el = document.querySelector(sel);
      if (!el) return null;
      return { sel, color: getComputedStyle(el).color, bg: effBg(el) };
    };
    return [
      grab('h1, .font-display'),
      grab('main p, .page-stack p'),
      grab('footer span'),
      grab('.nav-tab'),
    ].filter(Boolean).map(({ sel, color, bg }) => `${sel}: ${ratio(color, bg)}:1`);
  });
  report.push(`contrast ${theme}: ${ratios.join(' | ')}`);
}
await browser.close();
console.log(report.join('\n'));
