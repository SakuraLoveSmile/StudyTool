<template>
  <div class="surface p-4 space-y-3" data-testid="running-tasks-panel">
    <!-- Header -->
    <div class="flex items-center justify-between gap-2 flex-wrap">
      <div class="flex items-center gap-2">
        <Activity class="w-4 h-4 text-accent" />
        <h3 class="text-sm font-semibold text-ink">运行任务摘要</h3>
        <span
          v-if="runningTasks.length > 0"
          class="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-medium bg-accent/15 text-accent animate-pulse"
        >
          {{ runningTasks.length }} 个进行中
        </span>
      </div>
      <button
        type="button"
        class="text-xs text-accent hover:text-accent font-medium hover:underline cursor-pointer"
        @click="openDrawer"
      >
        打开任务监控
      </button>
    </div>

    <!-- 轮询中断提示 -->
    <div
      v-if="pollFailed"
      class="flex items-center gap-2 px-3 py-2 rounded-lg bg-warn/10 border border-warn/30 text-warn text-xs"
    >
      <AlertTriangle class="w-3.5 h-3.5 shrink-0" />
      <span>状态更新中断，正在自动重试…</span>
      <span v-if="lastSyncedAt" class="text-warn">
        最后成功更新：{{ lastSyncedClock }}
      </span>
    </div>

    <!-- 无任务 -->
    <div
      v-if="visibleTasks.length === 0 && !pollFailed"
      class="text-xs text-faint py-2"
    >
      当前没有正在执行或等待处理的任务。
    </div>

    <!-- Task Rows -->
    <div v-else class="space-y-2.5">
      <button
        v-for="task in visibleTasks"
        :key="task.id"
        type="button"
        class="w-full text-left p-3 rounded-xl border transition-colors cursor-pointer hover:border-accent/40 hover:bg-accent/10"
        :class="task.status === 'partial' || task.status === 'failed'
          ? 'border-err/30 bg-err/10'
          : 'border-line bg-surface'"
        @click="openTaskDetail(task.id)"
      >
        <!-- Title Row -->
        <div class="flex items-center gap-2 flex-wrap">
          <span
            class="w-2 h-2 rounded-full shrink-0"
            :class="task.status === 'pending'
              ? 'bg-amber-500'
              : (task.status === 'partial' || task.status === 'failed')
                ? 'bg-rose-500'
                : 'bg-brand-500 animate-pulse'"
          />
          <span class="text-sm font-semibold text-ink">{{ taskTypeLabel(task.task_type) }}</span>
          <span
            class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium"
            :class="statusBadgeClass(task.status)"
          >
            {{ statusLabel(task.status) }}
          </span>
          <span class="ml-auto text-[11px] text-faint font-mono">
            {{ formatDuration(getLiveElapsed(task)) }}
          </span>
        </div>

        <!-- Batch Counters -->
        <div class="mt-1.5 flex items-center gap-3 flex-wrap text-[11px]">
          <span v-if="summaryOf(task)" class="text-sub">
            批次：
            <span class="text-ok font-semibold">成功 {{ summaryOf(task)!.succeeded }}</span>
            <template v-if="summaryOf(task)!.failed > 0">
              · <span class="text-err font-semibold">失败 {{ summaryOf(task)!.failed }}</span>
            </template>
            <template v-if="summaryOf(task)!.skipped > 0">
              · <span class="text-sub">跳过 {{ summaryOf(task)!.skipped }}</span>
            </template>
            / 总 {{ summaryOf(task)!.total ?? '?' }}
          </span>
          <span v-else-if="task.progress_detail?.total" class="text-sub">
            已处理 {{ task.progress_detail.succeeded }}/{{ task.progress_detail.total }}
            {{ unitName(task.progress_detail.unit) }}
          </span>
          <span v-if="(task.progress_detail?.resultCount || 0) > 0" class="text-accent">
            识别结果 {{ task.progress_detail!.resultCount }}
          </span>
          <span v-if="isLegacyDetail(task)" class="text-faint">
            详细调用状态不可用
          </span>
        </div>

        <!-- 阻塞/并发请求明细（默认直接展示，无需展开） -->
        <div v-if="blockingLines(task).length > 0" class="mt-2 space-y-1">
          <div
            v-for="(line, idx) in blockingLines(task)"
            :key="idx"
            class="text-[11px] leading-relaxed flex items-start gap-1.5"
            :class="line.tone === 'warn' ? 'text-warn' : (line.tone === 'error' ? 'text-err' : 'text-accent')"
          >
            <span class="mt-1 w-1.5 h-1.5 rounded-full shrink-0"
              :class="line.tone === 'warn' ? 'bg-amber-500' : (line.tone === 'error' ? 'bg-rose-500' : 'bg-brand-500 animate-pulse')" />
            <span>{{ line.text }}</span>
          </div>
        </div>

        <!-- 等待过久 / 心跳中断提示 -->
        <div v-if="hintsFor(task).length > 0" class="mt-1.5 space-y-1">
          <div
            v-for="(hint, idx) in hintsFor(task)"
            :key="'h' + idx"
            class="text-[11px] text-warn bg-warn/10 border border-warn/20 rounded px-2 py-1"
          >
            {{ hint }}
          </div>
        </div>

        <!-- Progress -->
        <div class="mt-2">
          <progress
            v-if="task.status === 'pending' || isIndeterminate(task)"
            class="w-full h-1.5 rounded-full"
          />
          <progress
            v-else
            class="w-full h-1.5 rounded-full"
            :value="task.progress"
            max="100"
          />
        </div>
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { Activity, AlertTriangle } from 'lucide-vue-next';
import { useTaskManager } from '../composables/useTaskManager';
import type { TaskStatus } from '../types';
import {
  blockingLinesFor, hintsForTask, isLegacyDetail, taskTypeLabel
} from '../utils/taskStatus';
import { formatDuration, secondsSince } from '../utils/time';

const {
  activeTasks,
  actionableTasks,
  fetchTasks,
  startPolling,
  pollFailed,
  lastSyncedAt,
  openDrawer,
  openTaskDetail
} = useTaskManager();

// 运行中 + 异常（部分失败/失败待处理）任务都在此展示，含资料解析任务
const visibleTasks = computed(() => [...activeTasks.value, ...actionableTasks.value]);
const runningTasks = computed(() => activeTasks.value);

const nowTick = ref(Date.now());
let ticker: any = null;

onMounted(async () => {
  await fetchTasks();
  startPolling();
  ticker = setInterval(() => {
    nowTick.value = Date.now();
  }, 1000);
});

onUnmounted(() => {
  if (ticker) clearInterval(ticker);
});

const lastSyncedClock = computed(() => {
  if (!lastSyncedAt.value) return '';
  const d = new Date(lastSyncedAt.value);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`;
});

const summaryOf = (task: TaskStatus) => task.progress_detail?.summary ?? null;

const unitName = (unit?: string): string => {
  const map: Record<string, string> = { page: '页', chunk: '个片段', batch: '个批次', question: '题' };
  return map[unit || ''] || '';
};

const statusLabel = (status: string): string => {
  const map: Record<string, string> = {
    pending: '等待队列',
    processing: '执行中',
    completed: '已完成',
    failed: '失败',
    partial: '部分完成',
    cancel_requested: '正在停止...',
    cancelled: '已取消'
  };
  return map[status] || status;
};

const statusBadgeClass = (status: string): string => {
  switch (status) {
    case 'processing': return 'bg-accent/10 text-accent border border-accent/30';
    case 'pending': return 'bg-warn/10 text-warn border border-warn/30';
    case 'partial': return 'bg-orange-50 text-orange-700 border border-orange-200';
    case 'failed': return 'bg-err/10 text-err border border-err/30';
    default: return 'bg-raised text-sub';
  }
};

const isIndeterminate = (task: TaskStatus): boolean => (
  !task.progress_detail?.total
);

const getLiveElapsed = (task: TaskStatus): number => {
  if (['processing', 'cancel_requested'].includes(task.status)) {
    const age = secondsSince(task.started_at || task.created_at, nowTick.value);
    if (age !== null) return age;
  }
  return task.elapsed_seconds || 0;
};

interface BlockingLine {
  text: string;
  tone: 'info' | 'warn' | 'error';
}

/** 默认展示的阻塞信息：并发请求分别列出 + 部分失败提示 */
const blockingLines = (task: TaskStatus): BlockingLine[] => blockingLinesFor(task, nowTick.value);

const hintsFor = (task: TaskStatus): string[] => hintsForTask(task, nowTick.value);
</script>
