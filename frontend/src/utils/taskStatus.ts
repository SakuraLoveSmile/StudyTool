import type { ExecUnitStatus, TaskStatus } from '../types';
import { secondsSince } from './time';

export const SERVICE_LABELS: Record<string, string> = {
  llm: '模型',
  ocr: 'OCR',
  embedding: '向量服务'
};

/** Human-readable labels for every background task surfaced in the UI. */
export const TASK_TYPE_LABELS: Record<string, string> = {
  parse_document: '资料文本解析',
  extract_questions: '真题题目识别',
  generate_card: '考点学习卡生成',
  generate_recall_cards: '主动回忆卡生成',
  extract_knowledge_points: '教材考点梳理',
  reindex_chunk: '切片语义更新'
};

export function taskTypeLabel(type: string): string {
  return TASK_TYPE_LABELS[type] || type;
}

export const ACTIVE_UNIT_STATES = new Set([
  'queued', 'waiting_slot', 'awaiting_response', 'processing', 'saving', 'retry_wait'
]);

export function serviceLabel(service?: string | null): string {
  return SERVICE_LABELS[service || ''] || '服务';
}

export function kindLabel(unit: ExecUnitStatus): string {
  switch (unit.kind) {
    case 'batch':
      return unit.index ? `第 ${unit.index} 批` : '批次';
    case 'page':
      return unit.index ? `第 ${unit.index} 页` : '页面';
    case 'chunk':
      return '切片';
    case 'card':
      return '学习卡';
    default:
      return unit.kind || '单元';
  }
}

export function unitStateLabel(unit: ExecUnitStatus): string {
  switch (unit.state) {
    case 'queued':
      return '排队等待';
    case 'waiting_slot':
      return `等待${serviceLabel(unit.service)}调用名额`;
    case 'awaiting_response':
      return `等待${serviceLabel(unit.service)}响应`;
    case 'processing':
      return '正在校验响应';
    case 'saving':
      return '正在校验 / 保存';
    case 'retry_wait':
      return '重试等待';
    case 'succeeded':
      return '已完成';
    case 'failed':
      return '失败';
    case 'skipped':
      return '已跳过（检查点复用）';
    case 'cancelled':
      return '已取消';
    default:
      return unit.state;
  }
}

/** 当前单元的等待秒数（仅在等待类状态下计时；终态停止计时） */
export function unitWaitSeconds(unit: ExecUnitStatus, nowMs: number): number | null {
  if (unit.state === 'awaiting_response') {
    return secondsSince(unit.requestStartedAt, nowMs);
  }
  if (unit.state === 'waiting_slot') {
    return secondsSince(unit.requestStartedAt || unit.startedAt, nowMs);
  }
  return null;
}

/**
 * 一行可读描述，例如：
 * “第 3 批，第 9—12 页，等待模型响应 82 秒”
 * “第 5 批，第 17—20 页，重试等待（第 2 次尝试）：服务商限流 (429)...”
 */
export function unitLine(unit: ExecUnitStatus, nowMs: number): string {
  const parts: string[] = [];
  const kind = kindLabel(unit);
  parts.push(kind);
  if (unit.scope && unit.kind !== 'card') parts.push(unit.scope);

  const wait = unitWaitSeconds(unit, nowMs);
  if (unit.state === 'retry_wait') {
    let line = `${parts.join('，')}，重试等待（第 ${unit.attempt ?? 1} 次尝试）`;
    if (unit.retryWaitSeconds) line += `，约 ${Math.round(unit.retryWaitSeconds)} 秒后重试`;
    if (unit.retryReason) line += `：${unit.retryReason}`;
    return line;
  }
  if (unit.state === 'failed') {
    let line = `${parts.join('，')}，失败（第 ${unit.attempt ?? 1} 次尝试）`;
    if (unit.error) line += `：${unit.error}`;
    return line;
  }
  if (wait !== null) {
    return `${parts.join('，')}，${unitStateLabel(unit)} ${wait} 秒`;
  }
  const action = unit.action || unitStateLabel(unit);
  return `${parts.join('，')}，${action}`;
}

export function activeUnitsOf(task: TaskStatus): ExecUnitStatus[] {
  const units = task.progress_detail?.execUnits || [];
  return units.filter(u => ACTIVE_UNIT_STATES.has(u.state));
}

/** 旧任务判定：有进度明细但既无汇总也无逐单元状态 */
export function isLegacyDetail(task: TaskStatus): boolean {
  const pd = task.progress_detail;
  if (!pd) return false;
  return !pd.summary && (!pd.execUnits || pd.execUnits.length === 0);
}

/** 心跳超过 20 秒未更新 → 执行器状态可能中断 */
export function heartbeatStale(task: TaskStatus, nowMs: number): boolean {
  if (!['processing', 'cancel_requested'].includes(task.status)) return false;
  const age = secondsSince(task.progress_detail?.heartbeatAt, nowMs);
  return age !== null && age > 20;
}

/** 请求等待超过 60 秒提示 */
export function requestWaitLong(task: TaskStatus, nowMs: number): boolean {
  if (task.status !== 'processing') return false;
  const units = activeUnitsOf(task);
  if (units.length > 0) {
    return units.some(u => {
      const w = unitWaitSeconds(u, nowMs);
      return w !== null && w > 60;
    });
  }
  const legacy = secondsSince(task.progress_detail?.requestStartedAt, nowMs);
  return legacy !== null && legacy > 60;
}

export interface BlockingLine {
  text: string;
  tone: 'info' | 'warn' | 'error';
}

/**
 * 默认即可见的阻塞信息：并发请求逐条列出（例如
 * “第 3 批，第 9—12 页，等待模型响应 82 秒”）+ 部分批次失败提示 + 旧任务回退。
 */
export function blockingLinesFor(task: TaskStatus, nowMs: number): BlockingLine[] {
  const lines: BlockingLine[] = [];

  for (const unit of activeUnitsOf(task)) {
    let tone: BlockingLine['tone'] = 'info';
    if (unit.state === 'retry_wait') tone = 'warn';
    if (unit.state === 'failed') tone = 'error';
    lines.push({ text: unitLine(unit, nowMs), tone });
  }

  const failedUnits = (task.progress_detail?.execUnits || []).filter(u => u.state === 'failed');
  if (failedUnits.length > 0 && !activeUnitsOf(task).some(u => u.state === 'failed')) {
    const lastFailed = failedUnits[failedUnits.length - 1];
    let text = `部分批次失败：共 ${failedUnits.length} 个批次失败`;
    if (lastFailed?.error) text += `，原因：${lastFailed.error}`;
    lines.push({ text, tone: 'error' });
  }

  // 旧任务回退：只有一份共享执行描述可用
  if (lines.length === 0 && !isLegacyDetail(task) && task.status === 'processing') {
    const pd = task.progress_detail;
    const wait = secondsSince(pd?.requestStartedAt, nowMs);
    if (pd?.currentScope && wait !== null) {
      lines.push({ text: `${pd.currentScope}，${pd?.currentAction || '处理中'} ${wait} 秒`, tone: 'info' });
    }
  }
  return lines;
}

/** 提示信息：心跳中断 / 请求等待过久（不伪造服务端进度或完成倒计时） */
export function hintsForTask(task: TaskStatus, nowMs: number): string[] {
  const hints: string[] = [];
  if (heartbeatStale(task, nowMs)) {
    hints.push('执行器状态可能中断（心跳超过 20 秒未更新）');
  }
  if (requestWaitLong(task, nowMs)) {
    hints.push('等待较久，尚未收到响应');
  }
  return hints;
}
