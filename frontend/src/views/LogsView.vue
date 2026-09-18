<template>
  <div class="space-y-4">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h2 class="font-display text-lg font-bold tracking-tight text-ink">运行日志</h2>
        <p class="mt-0.5 text-xs text-sub">
          测试版诊断日志 · 只读 · 仅保存诊断元数据（不含资料原文、提示词、模型回答与密钥）
        </p>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <button
          type="button"
          class="btn btn-sm btn-secondary gap-1.5"
          :disabled="busy"
          @click="refresh(true)"
        >
          <RefreshCw class="h-3.5 w-3.5" :class="{ 'animate-spin': busy }" />手动刷新
        </button>
        <label
          class="btn btn-sm btn-secondary gap-1.5"
        >
          <input v-model="autoRefresh" type="checkbox" class="h-3.5 w-3.5 accent-accent" />
          自动刷新（3秒）
        </label>
        <button
          type="button"
          class="btn btn-sm btn-secondary gap-1.5"
          :disabled="!isDesktop"
          @click="openLogDirectory"
        >
          <FolderOpen class="h-3.5 w-3.5" />打开日志目录
        </button>
        <button
          type="button"
          class="btn btn-sm btn-primary gap-1.5"
          :disabled="!isDesktop || exporting"
          @click="exportDiagnostics"
        >
          <Download class="h-3.5 w-3.5" />{{ exporting ? '导出中…' : '导出诊断包' }}
        </button>
      </div>
    </div>

    <div
      v-if="!isDesktop"
      class="rounded-xl border border-warn/30 bg-warn/10 p-4 text-xs text-warn"
    >
      日志查看与导出仅在桌面客户端可用。浏览器版本不提供日志入口，也不开放 HTTP 日志接口。
    </div>

    <div v-if="isDesktop" class="surface p-3">
      <div class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <label class="text-xs">
          <span class="mb-1 block font-medium text-sub">时间范围</span>
          <select v-model="rangeKey" class="w-full rounded-lg border border-line px-2 py-1.5 text-xs">
            <option value="1h">最近 1 小时</option>
            <option value="24h">最近 24 小时（默认）</option>
            <option value="7d">最近 7 天（上限）</option>
          </select>
        </label>
        <label class="text-xs">
          <span class="mb-1 block font-medium text-sub">会话</span>
          <select v-model="sessionId" class="w-full rounded-lg border border-line px-2 py-1.5 text-xs">
            <option value="">全部会话</option>
            <option v-if="meta" :value="meta.sessionId">本次启动（{{ shortId(meta.sessionId) }}）</option>
          </select>
        </label>
        <label class="text-xs">
          <span class="mb-1 block font-medium text-sub">任务编号</span>
          <!-- 绑定原始输入，300 ms 防抖后才同步到生效筛选 -->
          <input
            v-model.trim="taskIdInput"
            type="text"
            placeholder="例如 task-xxxxxxxx"
            class="w-full rounded-lg border border-line px-2 py-1.5 text-xs"
          />
        </label>
        <label class="text-xs">
          <span class="mb-1 block font-medium text-sub">关键词</span>
          <!-- 绑定原始输入，300 ms 防抖后才同步到生效筛选 -->
          <input
            v-model.trim="keywordInput"
            type="text"
            placeholder="事件名或错误摘要"
            class="w-full rounded-lg border border-line px-2 py-1.5 text-xs"
          />
        </label>
        <div class="text-xs">
          <span class="mb-1 block font-medium text-sub">级别</span>
          <div class="flex flex-wrap gap-2">
            <label v-for="level in allLevels" :key="level" class="inline-flex items-center gap-1">
              <input v-model="levels" type="checkbox" :value="level" class="h-3.5 w-3.5 accent-accent" />
              <span>{{ level }}</span>
            </label>
          </div>
        </div>
        <div class="text-xs">
          <span class="mb-1 block font-medium text-sub">来源</span>
          <div class="flex flex-wrap gap-2">
            <label v-for="source in allSources" :key="source" class="inline-flex items-center gap-1">
              <input v-model="sources" type="checkbox" :value="source" class="h-3.5 w-3.5 accent-accent" />
              <span>{{ sourceLabel(source) }}</span>
            </label>
          </div>
        </div>
      </div>
      <p v-if="meta" class="mt-2 text-[11px] text-faint">
        版本 {{ meta.appVersion }} · {{ meta.platform }}/{{ meta.arch }} · 每页 {{ meta.pageSize }} 条 · 日志目录
        {{ meta.logDir }}
      </p>
    </div>

    <div v-if="isDesktop" class="surface">
      <div class="flex flex-wrap items-center justify-between gap-2 border-b border-line px-3 py-2 text-xs text-sub">
        <span v-if="queryError">
          查询失败 · {{ entries.length ? '下方保留上次成功读取的记录，可能不是最新结果' : '未取得日志结果' }}
        </span>
        <span v-else>
          共 {{ total }} 条 · 第 {{ page }} 页<template v-if="scanTruncated"> · 扫描结果已截断</template>
          <template v-if="skippedLines > 0"> · 已跳过 {{ skippedLines }} 条超大/损坏记录</template>
          <template v-if="unavailable.length"> · 未找到日志文件（可能尚未生成）：{{ unavailable.map(sourceLabel).join('、') }}</template>
        </span>
        <div class="flex items-center gap-2">
          <!-- 等待中仍可翻页：翻页会取消在途请求并请求目标页 -->
          <button
            type="button"
            class="cursor-pointer rounded-lg border border-line px-2 py-1 hover:bg-raised disabled:cursor-not-allowed disabled:opacity-40"
            :disabled="page <= 1"
            @click="goToPage(page - 1)"
          >
            上一页
          </button>
          <button
            type="button"
            class="cursor-pointer rounded-lg border border-line px-2 py-1 hover:bg-raised disabled:cursor-not-allowed disabled:opacity-40"
            :disabled="!hasMore"
            @click="goToPage(page + 1)"
          >
            下一页
          </button>
        </div>
      </div>

      <p v-if="queryError || errorMessage" role="alert" class="border-b border-err/30 bg-err/10 px-3 py-2 text-xs text-err">
        {{ queryError || errorMessage }}
        <span v-if="queryError">请根据错误信息处理后，点击“手动刷新”重试。</span>
      </p>

      <!-- 加载中（尚无结果）只提示正在读取，绝不显示成"没有日志记录" -->
      <div v-if="busy && entries.length === 0" class="px-3 py-10 text-center text-xs text-faint">
        正在读取日志…
      </div>

      <!--
        空状态只在"确实查完了、没有报错、结果为空"时出现；失败时保留上一次结果，
        因此不会与"没有日志"混淆。
      -->
      <div
        v-else-if="!busy && !queryError && !errorMessage && entries.length === 0"
        class="px-3 py-10 text-center text-xs text-faint"
      >
        当前筛选条件下没有日志记录。
      </div>

      <ul v-else class="divide-y divide-line">
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
                <span class="font-mono text-faint">{{ formatTime(entry.timestamp_ms || entry.timestamp) }}</span>
                <span class="rounded bg-raised px-1 text-[10px] text-sub">{{ sourceLabel(entry.source) }}</span>
                <span class="font-medium text-ink">{{ entry.event }}</span>
              </div>
              <p class="mt-0.5 break-words text-xs text-sub">{{ entry.message || '—' }}</p>
              <div class="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-faint">
                <span v-if="entry.task_id">任务 {{ entry.task_id }}</span>
                <span v-if="entry.session_id">会话 {{ shortId(entry.session_id) }}</span>
                <button
                  type="button"
                  class="cursor-pointer text-accent hover:underline"
                  @click="toggleDetail(entryKey(entry))"
                >
                  {{ expanded === entryKey(entry) ? '收起详情' : '查看详情' }}
                </button>
                <button
                  type="button"
                  class="cursor-pointer text-accent hover:underline"
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
                class="mt-2 max-h-72 overflow-auto rounded-lg bg-sink p-3 text-[11px] leading-relaxed text-on-sink"
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
import {
  markLogPageMounted,
  reportLogQueryFailed,
  reportLogQueryOk,
  reportLogQueryStart,
} from '../composables/useLogNavigation';

const RANGE_MS: Record<string, number> = {
  '1h': 60 * 60 * 1000,
  '24h': 24 * 60 * 60 * 1000,
  '7d': 7 * 24 * 60 * 60 * 1000,
};
const AUTO_REFRESH_MS = 3000;
/** 输入框防抖窗口：连续输入只在停下 300 ms 后发一次查询。 */
const INPUT_DEBOUNCE_MS = 300;
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
const queryError = ref('');
const scanTruncated = ref(false);
const skippedLines = ref(0);
const unavailable = ref<string[]>([]);
const expanded = ref<string | null>(null);
const copiedKey = ref<string | null>(null);

const rangeKey = ref('24h');
const levels = ref<string[]>([]);
const sources = ref<string[]>([]);
const sessionId = ref('');
// 输入框绑定"原始输入"，防抖后同步到下面的"生效筛选"，由生效筛选触发查询。
const taskIdInput = ref('');
const keywordInput = ref('');
const taskId = ref('');
const keyword = ref('');
const autoRefresh = ref(false);

const api = computed(() => (typeof window === 'undefined' ? undefined : window.desktopAPI));
const isDesktop = computed(() => Boolean(api.value?.isDesktop && api.value?.queryLogs));
const allLevels = ALL_LEVELS;
const allSources = ALL_SOURCES;

interface PendingTimer {
  current: ReturnType<typeof setTimeout> | null;
}

let autoRefreshTimer: ReturnType<typeof setTimeout> | null = null;
const taskIdDebounce: PendingTimer = { current: null };
const keywordDebounce: PendingTimer = { current: null };

// 请求身份：seq 决定哪个响应可以写入界面；requestId 用于真正取消主进程里正在跑的查询。
let seq = 0;
let requestCounter = 0;
let inFlightRequestId: string | null = null;
let disposed = false;
/** T2a：首次查询埋点只发一次（取消/被取代不算终结，留给下一次真实结果）。 */
let firstQueryTracked = false;

/**
 * 请求编号与主进程的 LOG_REQUEST_ID_PATTERN（/^[A-Za-z0-9_-]{1,64}$/）兼容；
 * 主进程会直接采用渲染进程提议的编号，因此取消接口能精确命中在途查询。
 */
function nextRequestId(): string {
  requestCounter += 1;
  return `q-${Date.now().toString(36)}-${requestCounter.toString(36)}`;
}

function rangeBounds(): { from: number; to: number } {
  const to = Date.now();
  return { from: to - (RANGE_MS[rangeKey.value] ?? RANGE_MS['24h']), to };
}

/**
 * 记录标识优先用主进程分配的 record_id；缺失时退回原来的拼接 key。
 * 去重（:key）、详情展开与复制都以同一套 key 比较，重复内容的日志不再冲突。
 */
function entryKey(entry: DesktopLogEntry): string {
  return entry.record_id || `${entry.timestamp_ms}|${entry.source}|${entry.event}|${entry.message}|${entry.task_id ?? ''}`;
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
      return 'bg-err/15 text-err';
    case 'WARN':
      return 'bg-warn/15 text-warn';
    case 'DEBUG':
      return 'bg-raised text-sub';
    default:
      return 'bg-accent/15 text-accent';
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

/** 失败时的中文提示：超时要明确"可重试"，取消/被取代根本不会走到这里。 */
function queryErrorMessage(code?: string, message?: string): string {
  const detail = message ? `（${message}）` : '';
  switch (code) {
    case 'LOG_QUERY_TIMEOUT':
      return `查询超时，可重试${detail}`;
    case 'LOG_WORKER_CRASHED':
      return `日志读取进程异常退出，可重试${detail}`;
    case 'LOG_INVALID_PARAMS':
      return `查询参数不合法，请调整筛选条件${detail}`;
    default:
      return message || '读取日志失败';
  }
}

async function loadMeta(): Promise<void> {
  try {
    const result = await api.value?.getLogMeta?.();
    if (result?.ok) meta.value = result;
  } catch {
    /* meta is cosmetic; the viewer still works without it */
  }
}

function pageHidden(): boolean {
  return typeof document !== 'undefined' && document.hidden;
}

/**
 * 取消在途查询：requestId 未知、preload 未暴露 cancelLogQuery 或取消失败都静默忽略。
 * 取消失败不影响正确性——旧响应仍会被本地 seq 整体丢弃。
 */
function cancelInFlight(): void {
  const desktop = api.value;
  const requestId = inFlightRequestId;
  inFlightRequestId = null;
  if (!requestId || !desktop?.cancelLogQuery) return;
  try {
    void desktop.cancelLogQuery(requestId).catch(() => {
      /* ignore */
    });
  } catch {
    /* ignore */
  }
}

/**
 * 统一的"筛选变更"入口：回到第 1 页重新查询。下拉框/级别/来源直接调用它，
 * 输入框则等 300 ms 防抖把原始输入同步到生效筛选后再调用。
 */
function applyFilterChange(): void {
  void refresh(true);
}

async function refresh(resetPage = false): Promise<void> {
  const desktop = api.value;
  if (!desktop?.queryLogs) return;
  if (resetPage) page.value = 1;

  // 本次查询取代上一次：先请主进程停掉 Worker，再用序号把旧响应整体丢弃。
  stopAutoRefresh();
  cancelInFlight();

  const mySeq = ++seq;
  const requestId = nextRequestId();
  inFlightRequestId = requestId;
  // 加载态在第一个 await 之前就置位，加载提示不会被"没有日志记录"抢走。
  busy.value = true;
  errorMessage.value = '';
  if (!firstQueryTracked) reportLogQueryStart();

  try {
    const result = await desktop.queryLogs({
      requestId,
      ...rangeBounds(),
      // Electron IPC cannot clone Vue's reactive array proxies.
      levels: [...levels.value],
      sources: [...sources.value],
      sessionId: sessionId.value || undefined,
      taskId: taskId.value || undefined,
      keyword: keyword.value || undefined,
      page: page.value,
      pageSize: meta.value?.pageSize ?? 200,
    });

    // 旧响应必须整体丢弃：不写列表、不动统计、不改错误提示。
    if (mySeq !== seq) return;
    if (inFlightRequestId === requestId) inFlightRequestId = null;

    if (!result?.ok) {
      // 取消/被取代属于正常控制流：不展示错误，也不改动任何已展示数据。
      if (result?.code !== 'LOG_QUERY_CANCELED' && result?.code !== 'LOG_QUERY_SUPERSEDED') {
        queryError.value = queryErrorMessage(result?.code, result?.message);
        if (!firstQueryTracked) {
          firstQueryTracked = true;
          reportLogQueryFailed(result?.code, result?.message);
        }
      }
      // 失败时保留上一次的结果与统计字段，避免显示成"没有日志记录"。
      return;
    }

    if (!firstQueryTracked) {
      firstQueryTracked = true;
      reportLogQueryOk(result.elapsedMs);
    }
    entries.value = result.entries ?? [];
    total.value = result.total ?? 0;
    hasMore.value = Boolean(result.hasMore);
    scanTruncated.value = Boolean(result.scanTruncated);
    skippedLines.value = result.skippedLines ?? 0;
    unavailable.value = result.unavailable ?? [];
    queryError.value = '';
    errorMessage.value = '';
  } catch (err: any) {
    // 抛错同样可能是旧请求：序号过期时什么都不做。
    if (mySeq !== seq) return;
    if (inFlightRequestId === requestId) inFlightRequestId = null;
    // 保留上次结果与统计字段，只提示错误。
    queryError.value = err?.message || '读取日志失败';
    if (!firstQueryTracked) {
      firstQueryTracked = true;
      reportLogQueryFailed('IPC_THROW', err?.message);
    }
  } finally {
    // 只有最新一次查询负责收尾：结束加载态，并安排"上次完成后 3 秒"的下一轮。
    if (mySeq === seq) {
      busy.value = false;
      if (!disposed) scheduleAutoRefresh();
    }
  }
}

function goToPage(next: number): void {
  const target = Math.max(1, next);
  if (target === page.value) return;
  page.value = target;
  expanded.value = null;
  // 等待期间翻页：refresh() 会取消在途请求并请求目标页。
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
    clearTimeout(autoRefreshTimer);
    autoRefreshTimer = null;
  }
}

/**
 * 自动刷新改为"上一次查询结束后再等 3 秒"：只有查询收尾（成功或失败）才会安排下一轮，
 * 因此上一轮还没结束就不会叠发下一轮。开关关闭、页面隐藏、组件卸载都不再安排。
 */
function scheduleAutoRefresh(): void {
  stopAutoRefresh();
  if (disposed || !autoRefresh.value || pageHidden()) return;
  autoRefreshTimer = setTimeout(() => {
    autoRefreshTimer = null;
    if (disposed || !autoRefresh.value || pageHidden()) return;
    void refresh();
  }, AUTO_REFRESH_MS);
}

/** 页面隐藏时暂停自动刷新并取消在途查询；恢复可见且开关仍开着时，重新安排 3 秒后刷新。 */
function handleVisibilityChange(): void {
  if (pageHidden()) {
    stopAutoRefresh();
    cancelInFlight();
    return;
  }
  if (autoRefresh.value) scheduleAutoRefresh();
}

/** 原始输入 → 生效筛选（300 ms 防抖）；内容没变就不触发查询。 */
function debounceInto(raw: string, effective: { value: string }, pending: PendingTimer): void {
  if (pending.current !== null) clearTimeout(pending.current);
  pending.current = setTimeout(() => {
    pending.current = null;
    if (effective.value === raw) return;
    effective.value = raw;
  }, INPUT_DEBOUNCE_MS);
}

function clearDebounces(): void {
  for (const pending of [taskIdDebounce, keywordDebounce]) {
    if (pending.current !== null) {
      clearTimeout(pending.current);
      pending.current = null;
    }
  }
}

watch(autoRefresh, (enabled) => {
  if (enabled) scheduleAutoRefresh();
  else stopAutoRefresh();
});

// 下拉框（时间范围/会话）、级别、来源：走统一的筛选变更入口，不加额外延迟。
watch([rangeKey, levels, sources, sessionId], applyFilterChange, { deep: true });

// 任务编号/关键词：先防抖，再由生效值触发查询。
watch(taskIdInput, (value) => debounceInto(value, taskId, taskIdDebounce));
watch(keywordInput, (value) => debounceInto(value, keyword, keywordDebounce));
watch([taskId, keyword], applyFilterChange);

onMounted(async () => {
  document.addEventListener('visibilitychange', handleVisibilityChange);
  // T2a：页面真正挂载的证据点，与外壳 nav-<id> 诊断链关联。
  markLogPageMounted();
  if (!isDesktop.value) return;
  // meta 与首屏查询并行：加载提示不必等 meta 返回。
  void loadMeta();
  await refresh();
});

// 离开日志页：摘掉监听、清掉全部计时器并取消在途查询，不留后台请求。
onBeforeUnmount(() => {
  disposed = true;
  document.removeEventListener('visibilitychange', handleVisibilityChange);
  stopAutoRefresh();
  clearDebounces();
  cancelInFlight();
});
</script>
