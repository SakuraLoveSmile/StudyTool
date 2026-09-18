<template>
  <!--
    外壳级"打开日志页"状态条：必须挂在 Navbar 与 <main> 之间（App.vue），
    不能放进 LogsView——懒加载 chunk 失败时页面组件根本渲染不出来。
  -->
  <div v-if="phase !== 'idle'" class="mx-auto w-full max-w-7xl px-4 pt-3 sm:px-6 lg:px-8" role="status" aria-live="polite">
    <!-- 进行中：点击即出现，告知导航已受理 -->
    <div
      v-if="phase === 'pending'"
      class="flex items-center gap-2.5 rounded-xl border border-accent/30 bg-accent/10 px-3.5 py-2.5 text-xs text-accent"
    >
      <Loader2 class="h-3.5 w-3.5 animate-spin" />
      <span>正在打开运行日志…</span>
    </div>

    <!-- 超过 10 秒：保留原页面，只提示，不强制刷新、不清草稿 -->
    <div
      v-else-if="phase === 'slow'"
      class="flex flex-wrap items-center gap-2.5 rounded-xl border border-warn/30 bg-warn/10 px-3.5 py-2.5 text-xs text-warn"
    >
      <Loader2 class="h-3.5 w-3.5 animate-spin" />
      <span class="min-w-0 flex-1">打开时间较长，仍在等待日志页加载。你可以继续等待或重试，当前页面内容不受影响。</span>
      <button type="button" class="btn-sm btn-secondary shrink-0" @click="retry">重试</button>
      <button type="button" class="shrink-0 rounded-lg p-1 text-warn/70 hover:bg-warn/15" aria-label="关闭提示" @click="dismiss">
        <X class="h-3.5 w-3.5" />
      </button>
    </div>

    <!-- 失败：错误摘要 + 重试 + 打开诊断目录（IPC 存在才显示该按钮） -->
    <div
      v-else
      class="flex flex-wrap items-center gap-2.5 rounded-xl border border-err/30 bg-err/10 px-3.5 py-2.5 text-xs text-err"
    >
      <AlertTriangle class="h-3.5 w-3.5 shrink-0" />
      <span class="min-w-0 flex-1">
        无法打开运行日志<template v-if="errorSummary">：{{ errorSummary }}</template>
      </span>
      <button type="button" class="btn-sm btn-secondary shrink-0" @click="retry">重试</button>
      <button
        v-if="canOpenLogDirectory"
        type="button"
        class="btn-sm btn-secondary shrink-0 gap-1"
        @click="openLogDirectory"
      >
        <FolderOpen class="h-3.5 w-3.5" />打开诊断目录
      </button>
      <button type="button" class="shrink-0 rounded-lg p-1 text-err/70 hover:bg-err/15" aria-label="关闭提示" @click="dismiss">
        <X class="h-3.5 w-3.5" />
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { AlertTriangle, FolderOpen, Loader2, X } from 'lucide-vue-next';
import {
  dismissLogNavigation,
  logNavState,
  retryLogNavigation,
} from '../composables/useLogNavigation';

const phase = computed(() => logNavState.phase);
const errorSummary = computed(() => logNavState.errorSummary);

// openLogDirectory 是受限 IPC，方法存在才显示按钮（浏览器构建 / 旧 preload 不暴露）。
const canOpenLogDirectory = computed(
  () => typeof window !== 'undefined' && typeof window.desktopAPI?.openLogDirectory === 'function'
);

function retry(): void {
  retryLogNavigation();
}

function dismiss(): void {
  dismissLogNavigation();
}

function openLogDirectory(): void {
  try {
    window.desktopAPI?.openLogDirectory?.();
  } catch {
    /* IPC 失败静默：按钮本身已做存在性检查，这里只是兜底 */
  }
}
</script>
