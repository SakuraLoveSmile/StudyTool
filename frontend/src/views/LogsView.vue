<template>
  <div class="space-y-4">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h2 class="text-lg font-bold tracking-tight text-slate-900">运行日志</h2>
        <p class="mt-0.5 text-xs text-slate-500">
          测试版诊断日志 · 只读 · 仅保存诊断元数据（不含资料原文、提示词、模型回答与密钥）
        </p>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <button
          type="button"
          class="inline-flex cursor-pointer items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
          :disabled="busy"
          @click="refresh(true)"
        >
          <RefreshCw class="h-3.5 w-3.5" :class="{ 'animate-spin': busy }" />手动刷新
        </button>
        <label
          class="inline-flex cursor-pointer items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700"
        >
          <input v-model="autoRefresh" type="checkbox" class="h-3.5 w-3.5 accent-blue-600" />
          自动刷新（3秒）
        </label>
        <button
          type="button"
          class="inline-flex cursor-pointer items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
          :disabled="!isDesktop"
          @click="openLogDirectory"
        >
          <FolderOpen class="h-3.5 w-3.5" />打开日志目录
        </button>
        <button
          type="button"
          class="inline-flex cursor-pointer items-center gap-1.5 rounded-lg bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
          :disabled="!isDesktop || exporting"
          @click="exportDiagnostics"
        >
          <Download class="h-3.5 w-3.5" />{{ exporting ? '导出中…' : '导出诊断包' }}
        </button>
      </div>
    </div>

    <div
      v-if="!isDesktop"
      class="rounded-xl border border-amber-200 bg-amber-50 p-4 text-xs text-amber-800"
    >
      日志查看与导出仅在桌面客户端可用。浏览器版本不提供日志入口，也不开放 HTTP 日志接口。
    </div>

    <div v-if="isDesktop" class="rounded-xl border border-slate-200 bg-white p-3">
      <div class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <label class="text-xs">
          <span class="mb-1 block font-medium text-slate-600">时间范围</span>
          <select v-model="rangeKey" class="w-full rounded-lg border border-slate-200 px-2 py-1.5 text-xs">
            <option value="1h">最近 1 小时</option>
            <option value="24h">最近 24 小时（默认）</option>
            <option value="7d">最近 7 天（上限）</option>
          </select>
        </label>
        <label class="text-xs">
          <span class="mb-1 block font-medium text-slate-600">会话</span>
          <select v-model="sessionId" class="w-full rounded-lg border border-slate-200 px-2 py-1.5 text-xs">
            <option value="">全部会话</option>
            <option v-if="meta" :value="meta.sessionId">本次启动（{{ shortId(meta.sessionId) }}）</option>
          </select>
        </label>
        <label class="text-xs">
          <span class="mb-1 block font-medium text-slate-600">任务编号</span>
          <input
            v-model.trim="taskId"
            type="text"
            placeholder="例如 task-xxxxxxxx"
            class="w-full rounded-lg border border-slate-200 px-2 py-1.5 text-xs"
          />
        </label>
        <label class="text-xs">
          <span class="mb-1 block font-medium text-slate-600">关键词</span>
          <input
            v-model.trim="keyword"
            type="text"
            placeholder="事件名或错误摘要"
            class="w-full rounded-lg border border-slate-200 px-2 py-1.5 text-xs"
          />
        </label>
        <div class="text-xs">
          <span class="mb-1 block font-medium text-slate-600">级别</span>
          <div class="flex flex-wrap gap-2">
            <label v-for="level in allLevels" :key="level" class="inline-flex items-center gap-1">
              <input v-model="levels" type="checkbox" :value="level" class="h-3.5 w-3.5 accent-blue-600" />
              <span>{{ level }}</span>
            </label>
          </div>
        </div>
        <div class="text-xs">
          <span class="mb-1 block font-medium text-slate-600">来源</span>
          <div class="flex flex-wrap gap-2">
            <label v-for="source in allSources" :key="source" class="inline-flex items-center gap-1">
              <input v-model="sources" type="checkbox" :value="source" class="h-3.5 w-3.5 accent-blue-600" />
              <span>{{ sourceLabel(source) }}</span>
            </label>
          </div>
        </div>
      </div>
      <p v-if="meta" class="mt-2 text-[11px] text-slate-400">
        版本 {{ meta.appVersion }} · {{ meta.platform }}/{{ meta.arch }} · 每页 {{ meta.pageSize }} 条 · 日志目录
        {{ meta.logDir }}
      </p>
    </div>

    <div v-if="isDesktop" class="rounded-xl border border-slate-200 bg-white">
      <div class="flex flex-wrap items-center justify-between gap-2 border-b border-slate-100 px-3 py-2 text-xs text-slate-500">
        <span>
          共 {{ total }} 条 · 第 {{ page }} 页<template v-if="scanTruncated"> · 扫描结果已截断</template>
          <template v-if="unavailable.length"> · 缺失文件：{{ unavailable.join('、') }}</template>
        </span>
        <div class="flex items-center gap-2">
          <button
            type="button"
            class="cursor-pointer rounded-lg border border-slate-200 px-2 py-1 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
            :disabled="page <= 1 || busy"
            @click="goToPage(page - 1)"
          >
            上一页
          </button>
          <button
            type="button"
            class="cursor-pointer rounded-lg border border-slate-200 px-2 py-1 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
            :disabled="!hasMore || busy"
            @click="goToPage(page + 1)"
          >
            下一页
          </button>
        </div>
      </div>

      <p v-if="errorMessage" class="border-b border-rose-100 bg-rose-50 px-3 py-2 text-xs text-rose-700">
        {{ errorMessage }}
      </p>

      <div v-if="entries.length === 0 && !busy" class="px-3 py-10 text-center text-xs text-slate-400">
        当前筛选条件下没有日志记录。
      </div>

      <ul v-else class="divide-y divide-slate-100">
        <li v-for="entry in entries" :key="entryKey(entry)" class="px-3 py-2">
          <div class="flex items-start gap-2">
            <span
              class="mt-0.5 inline-flex shrink-0 items-center rounded-md px-1.5 py-0.5 text-[10px] font-bold"
              :class="levelClass(entry.level)"
            >
              {{ entry.level }}
            </span>
            <div class="min-w-0 flex-1">
              <div class="flex flex-wrap items-baseline gap-x-2 text-xs">
                <span class="font-mono text-slate-400">{{ formatTime(entry.timestamp_ms || entry.timestamp) }}</span>
                <span class="rounded bg-slate-100 px-1 text-[10px] text-slate-500">{{ sourceLabel(entry.source) }}</span>
                <span class="font-medium text-slate-800">{{ entry.event }}</span>
              </div>
              <p class="mt-0.5 break-words text-xs text-slate-600">{{ entry.message || '—' }}</p>
              <div class="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-slate-400">
                <span v-if="entry.task_id">任务 {{ entry.task_id }}</span>
                <span v-if="entry.session_id">会话 {{ shortId(entry.session_id) }}</span>
                <button
                  type="button"
                  class="cursor-pointer text-blue-600 hover:underline"
                  @click="toggleDetail(entryKey(entry))"
                >
                  {{ expanded === entryKey(entry) ? '收起详情' : '查看详情' }}
                </button>
                <button
                  type="button"
                  class="cursor-pointer text-blue-600 hover:underline"
                  @click="copyEntry(entry)"
                >
                  {{ copiedKey === entryKey(entry) ? '已复制' : '复制' }}
                </button>
              </div>

              <!--
                日志详情按 T4 要求默认纳入反馈截图遮罩：整个详情子树标注
                data-feedback-capture-mask，反馈组件克隆与最终位图都会遮挡。
              -->
              <pre
                v-if="expanded === entryKey(entry)"
                data-feedback-capture-mask
                class="mt-2 max-h-72 overflow-auto rounded-lg bg-slate-900 p-3 text-[11px] leading-relaxed text-slate-100"
              >{{ JSON.stringify(entry, null, 2) }}</pre>
            </div>
          </div>
        </li>
      </ul>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { Download, FolderOpen, RefreshCw } from 'lucide-vue-next';
import type { DesktopLogEntry, DesktopLogMeta } from '../composables/useDesktop';

const RANGE_MS: Record<string, number> = {
  '1h': 60 * 60 * 1000,
  '24h': 24 * 60 * 60 * 1000,
  '7d': 7 * 24 * 60 * 60 * 1000,
};
const AUTO_REFRESH_MS = 3000;
const ALL_LEVELS = ['DEBUG', 'INFO', 'WARN', 'ERROR'];
const ALL_SOURCES = ['desktop', 'backend', 'frontend'];

const meta = ref<DesktopLogMeta | null>(null);
const entries = ref<DesktopLogEntry[]>([]);
const total = ref(0);
const page = ref(1);
const hasMore = ref(false);
const busy = ref(false);
const exporting = ref(false);
const errorMessage = ref('');
const scanTruncated = ref(false);
const unavailable = ref<string[]>([]);
const expanded = ref<string | null>(null);
const copiedKey = ref<string | null>(null);

const rangeKey = ref('24h');
const levels = ref<string[]>([]);
const sources = ref<string[]>([]);
const sessionId = ref('');
const taskId = ref('');
const keyword = ref('');
const autoRefresh = ref(false);

const api = computed(() => (typeof window === 'undefined' ? undefined : window.desktopAPI));
const isDesktop = computed(() => Boolean(api.value?.isDesktop && api.value?.queryLogs));
const allLevels = ALL_LEVELS;
const allSources = ALL_SOURCES;

let autoRefreshTimer: ReturnType<typeof setInterval> | null = null;

function rangeBounds(): { from: number; to: number } {
  const to = Date.now();
  return { from: to - (RANGE_MS[rangeKey.value] ?? RANGE_MS['24h']), to };
}

function entryKey(entry: DesktopLogEntry): string {
  return `${entry.timestamp_ms}|${entry.source}|${entry.event}|${entry.message}|${entry.task_id ?? ''}`;
}

function shortId(value: string): string {
  return value.length > 12 ? `${value.slice(0, 8)}…` : value;
}

function sourceLabel(source: string): string {
  return { desktop: '桌面', backend: '后台', frontend: '前端' }[source] ?? source;
}

function levelClass(level: string): string {
  switch (level) {
    case 'ERROR':
      return 'bg-rose-100 text-rose-700';
    case 'WARN':
      return 'bg-amber-100 text-amber-700';
    case 'DEBUG':
      return 'bg-slate-100 text-slate-500';
    default:
      return 'bg-blue-100 text-blue-700';
  }
}

function formatTime(value: number | string): string {
  const ms = typeof value === 'number' && value > 0 ? value : Date.parse(String(value));
  if (!Number.isFinite(ms) || ms <= 0) return '时间未知';
  const date = new Date(ms);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(
    date.getMinutes()
  )}:${pad(date.getSeconds())}`;
}

async function loadMeta(): Promise<void> {
  try {
    const result = await api.value?.getLogMeta?.();
    if (result?.ok) meta.value = result;
  } catch {
    /* meta is cosmetic; the viewer still works without it */
  }
}

async function refresh(resetPage = false): Promise<void> {
  const desktop = api.value;
  if (!desktop?.queryLogs) return;
  if (resetPage) page.value = 1;
  busy.value = true;
  errorMessage.value = '';
  try {
    const result = await desktop.queryLogs({
      ...rangeBounds(),
      levels: levels.value,
      sources: sources.value,
      sessionId: sessionId.value || undefined,
      taskId: taskId.value || undefined,
      keyword: keyword.value || undefined,
      page: page.value,
      pageSize: meta.value?.pageSize ?? 200,
    });
    entries.value = result?.entries ?? [];
    total.value = result?.total ?? 0;
    hasMore.value = Boolean(result?.hasMore);
    scanTruncated.value = Boolean(result?.scanTruncated);
    unavailable.value = result?.unavailable ?? [];
  } catch (err: any) {
    errorMessage.value = err?.message || '读取日志失败';
    entries.value = [];
  } finally {
    busy.value = false;
  }
}

function goToPage(next: number): void {
  page.value = Math.max(1, next);
  expanded.value = null;
  void refresh();
}

function toggleDetail(key: string): void {
  expanded.value = expanded.value === key ? null : key;
}

async function copyEntry(entry: DesktopLogEntry): Promise<void> {
  const key = entryKey(entry);
  try {
    await navigator.clipboard.writeText(JSON.stringify(entry, null, 2));
    copiedKey.value = key;
    setTimeout(() => {
      if (copiedKey.value === key) copiedKey.value = null;
    }, 1500);
  } catch {
    errorMessage.value = '复制失败，请手动选择详情内容。';
  }
}

function openLogDirectory(): void {
  try {
    api.value?.openLogDirectory?.();
  } catch {
    /* ignore */
  }
}

async function exportDiagnostics(): Promise<void> {
  const desktop = api.value;
  if (!desktop?.exportDiagnostics) return;
  exporting.value = true;
  errorMessage.value = '';
  try {
    const result = await desktop.exportDiagnostics(rangeBounds());
    if (result?.canceled) {
      errorMessage.value = '已取消导出，未生成任何文件。';
    } else if (result?.ok) {
      errorMessage.value = `诊断包已导出：${result.path}（${result.entries ?? 0} 条记录${
        result.truncated ? '，原始日志超限已截断' : ''
      }）`;
    } else {
      errorMessage.value = result?.message || '导出失败';
    }
  } catch (err: any) {
    errorMessage.value = err?.message || '导出失败';
  } finally {
    exporting.value = false;
  }
}

function stopAutoRefresh(): void {
  if (autoRefreshTimer !== null) {
    clearInterval(autoRefreshTimer);
    autoRefreshTimer = null;
  }
}

function startAutoRefresh(): void {
  stopAutoRefresh();
  autoRefreshTimer = setInterval(() => {
    // Leaving the page also stops this, but guard anyway: never poll a hidden tab.
    if (typeof document !== 'undefined' && document.hidden) return;
    void refresh();
  }, AUTO_REFRESH_MS);
}

watch(autoRefresh, (enabled) => {
  if (enabled) startAutoRefresh();
  else stopAutoRefresh();
});

// Filter changes reload from page 1; deep filters would otherwise land on an empty page.
watch([rangeKey, levels, sources, sessionId, taskId, keyword], () => {
  void refresh(true);
});

onMounted(async () => {
  await loadMeta();
  if (isDesktop.value) await refresh();
});

// Auto-refresh must not survive navigating away from the log page.
onBeforeUnmount(stopAutoRefresh);
</script>
