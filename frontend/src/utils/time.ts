/**
 * 时间工具：后端时间统一按 UTC 解析，本地时区显示。
 * - SQLite CURRENT_TIMESTAMP 形如 "YYYY-MM-DD HH:MM:SS"（UTC，无时区标记）
 * - 执行器生成的 ISO 串形如 "2026-09-09T08:00:00Z"（已带 Z）
 */

export function parseServerTime(ts?: string | null): Date | null {
  if (!ts) return null;
  let s = String(ts).trim();
  if (!s) return null;
  // 无时区标记的 UTC 时间补 Z；已带 Z / +00:00 的原样解析
  if (/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?$/.test(s)) {
    s = s.replace(' ', 'T') + 'Z';
  }
  const d = new Date(s);
  return isNaN(d.getTime()) ? null : d;
}

export function secondsSince(ts?: string | null, nowMs: number = Date.now()): number | null {
  const d = parseServerTime(ts);
  if (!d) return null;
  return Math.max(0, Math.round((nowMs - d.getTime()) / 1000));
}

export function formatDuration(sec?: number | null): string {
  if (sec === undefined || sec === null || isNaN(sec)) return '0 秒';
  const s = Math.round(sec);
  if (s < 60) return `${s} 秒`;
  if (s < 3600) return `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
  return `${Math.floor(s / 3600)} 小时 ${Math.floor((s % 3600) / 60)} 分`;
}

export function formatClock(ts?: string | null): string {
  const d = parseServerTime(ts);
  if (!d) return '';
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`;
}

export function formatDateTime(ts?: string | null): string {
  const d = parseServerTime(ts);
  if (!d) return '';
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}
