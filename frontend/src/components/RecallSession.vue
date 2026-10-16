<template>
  <section class="space-y-4" :class="{ 'android-recall': isAndroidApp, 'recall-focused': isAndroidApp && focused }" aria-labelledby="recall-session-title">
    <div class="surface p-4 sm:p-5 flex flex-wrap items-center justify-between gap-3">
      <div>
        <div class="flex items-center gap-2">
          <span class="inline-flex items-center rounded-full bg-accent/10 px-2 py-0.5 text-[11px] font-semibold text-accent">今日回忆</span>
          <span v-if="attempt && attempt.chapter_name" class="text-xs text-sub">{{ attempt.chapter_name }}</span>
        </div>
        <h2 id="recall-session-title" class="mt-1 text-lg font-bold tracking-tight text-ink">
          {{ attempt ? (isAndroidApp && !attempt.total ? '当前回忆' : `第 ${attempt.position || 1} / ${attempt.total || 1} 张`) : dayComplete ? '今日学习已完成' : roundComplete ? '本轮学习已完成' : isAndroidApp && sessionState === 'active' ? '当前回忆队列' : '准备开始' }}
        </h2>
      </div>
      <div class="flex items-center gap-2">
        <button v-if="attempt" type="button" class="btn btn-secondary text-xs" :disabled="busy" @click="emit('pause')">
          暂停 <kbd v-if="!isAndroidApp" class="ml-1 opacity-60 font-mono text-[10px]">Esc</kbd>
        </button>
        <button v-if="!isAndroidApp && (attempt?.completed || attempt?.status === 'rated')" type="button" class="btn btn-secondary text-xs" :disabled="busy" @click="emit('skip')">跳过</button>
      </div>
    </div>

    <div v-if="!attempt" class="surface p-8 sm:p-12 text-center">
      <div class="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-ok/10 text-ok">
        <BookOpen v-if="isAndroidApp && !roundComplete && !dayComplete" class="h-6 w-6" /><Check v-else class="h-6 w-6" />
      </div>
      <h3 class="font-display text-lg font-semibold text-ink">{{ isAndroidApp && !roundComplete && !dayComplete ? (sessionState === 'active' ? '当前范围暂无可学习卡片' : sessionState === 'paused' ? '歇一会儿，再接着学' : '准备好，回忆今天的知识') : dayComplete ? '今日学习已完成' : roundComplete ? '本轮学习已完成' : '今日回忆已完成' }}</h3>
      <p class="mt-1 text-xs text-sub">{{ isAndroidApp && !roundComplete && !dayComplete ? (sessionState === 'active' ? '请展开“学习范围与设置”，确认章节已发布卡片，并设置每日新卡上限（1–100）后保存。再暂停并继续学习，重新准备队列。' : sessionState === 'paused' ? '点击上方“继续今日学习”，接着上次的节奏。' : '先确认学习范围，再点击“开始今日学习”。还没有卡片时，可以去教材页生成并审核发布。') : dayComplete ? '今天的任务已经完成，可以整理卡片或明天继续。' : roundComplete ? (finalRound ? '本日三轮回忆已完成，确认后结束今日学习。' : '可以进入下一轮，或返回卡片库整理内容。') : '可以返回卡片库整理内容，或稍后再来复习。' }}</p>
      <button v-if="roundComplete && !dayComplete" type="button" class="btn btn-primary mt-4" :disabled="busy" @click="emit('advance')">
        {{ finalRound ? '完成今日学习' : '进入下一轮' }} <kbd v-if="!isAndroidApp" class="ml-1.5 rounded bg-white/20 px-1.5 py-0.5 text-[10px] font-mono">Space</kbd>
      </button>
    </div>

    <template v-else>
      <div class="surface overflow-hidden border-t-4 border-t-accent">
        <div class="p-5 sm:p-8 space-y-5 recall-body" :class="{ 'has-answer': attempt.revealed }">
          <div class="recall-question space-y-5">
          <div class="flex items-start justify-between gap-3">
            <span class="text-xs font-semibold uppercase tracking-wider text-faint">问题</span>
            <span v-if="attempt.rating" class="rounded-full bg-ok/10 px-2 py-0.5 text-[11px] font-semibold text-ok">
              {{ ratingLabel(attempt.rating) }}
            </span>
          </div>
          <p class="whitespace-pre-wrap break-words font-display text-base sm:text-xl font-semibold leading-relaxed text-ink">{{ attempt.prompt }}</p>
          </div>

          <div v-if="!attempt.revealed" class="rounded-xl border border-dashed border-accent/30 bg-accent/10 p-4 sm:p-5">
            <p class="text-sm leading-relaxed text-accent">先在心里或纸上作答，再揭示参考答案。主动回忆比重复阅读更有效。</p>
            <p v-if="showHints && attempt.hint" class="mt-2 text-xs text-accent"><span class="font-semibold">提示：</span>{{ attempt.hint }}</p>
            <button type="button" class="btn btn-primary mt-4 w-full sm:w-auto" :disabled="busy" @click="emit('reveal')">
              {{ busy ? '处理中…' : '揭示答案' }} <kbd v-if="!busy && !isAndroidApp" class="ml-1.5 rounded bg-white/20 px-1.5 py-0.5 text-[10px] font-mono">Space</kbd>
            </button>
          </div>

          <div v-else class="space-y-4 border-t border-line pt-5 recall-answer">
            <div>
              <span class="text-xs font-semibold uppercase tracking-wider text-faint">参考答案</span>
              <p class="mt-2 whitespace-pre-wrap break-words text-sm sm:text-base leading-relaxed text-ink">{{ attempt.answer || '暂无参考答案' }}</p>
            </div>
            <div v-if="sourceExcerpt" class="rounded-xl border border-ok/30 bg-ok/10 p-3.5 text-xs text-ok">
              <span class="font-semibold">来源摘录：</span>
              <span class="whitespace-pre-wrap break-words">{{ sourceExcerpt }}</span>
            </div>
            <div>
              <p class="mb-2 text-xs font-semibold text-sub">这次回忆的结果</p>
              <div class="grid grid-cols-1 gap-2 sm:grid-cols-3">
                <button
                  v-for="option in ratingOptions"
                  :key="option.value"
                  type="button"
                  class="min-h-[42px] rounded-lg border px-3 py-2 text-xs font-semibold transition-colors disabled:opacity-50 flex items-center justify-center gap-1.5"
                  :class="option.className"
                  :disabled="busy || Boolean(attempt.rating) || attempt.completed"
                  @click="emit('rate', option.value)"
                >
                  <span v-if="!isAndroidApp" class="rounded bg-black/10 dark:bg-white/10 px-1.5 py-0.5 text-[10px] font-mono font-bold">{{ option.keyHint }}</span>
                  <span>{{ option.label }}</span>
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div class="surface p-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div v-if="!isAndroidApp || attempt.total > 0" class="text-xs text-sub">
          <span>完成进度</span>
          <div class="mt-1.5 h-2 w-full min-w-[180px] overflow-hidden rounded-full bg-raised sm:w-64">
            <div class="h-full rounded-full bg-brand-600 transition-all" :style="{ width: `${progress}%` }" />
          </div>
        </div>
        <button v-if="attempt.revealed && (attempt.completed || attempt.status === 'rated')" type="button" class="btn btn-primary" :disabled="busy" @click="emit('next')">
          下一张 <kbd v-if="!isAndroidApp" class="ml-1.5 rounded bg-white/20 px-1.5 py-0.5 text-[10px] font-mono">Space</kbd>
        </button>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted } from 'vue';
import { BookOpen, Check } from 'lucide-vue-next';
import { isAndroidApp } from '../utils/clientPlatform';
import type { AttemptRating, TodayAttempt } from '../types/learning';

interface Props {
  attempt?: TodayAttempt | null;
  busy?: boolean;
  sourceExcerpt?: string | null;
  showHints?: boolean;
  roundComplete?: boolean;
  dayComplete?: boolean;
  finalRound?: boolean;
  focused?: boolean;
  sessionState?: 'idle' | 'active' | 'paused' | 'completed';
}

const props = withDefaults(defineProps<Props>(), {
  attempt: null,
  busy: false,
  sourceExcerpt: null,
  showHints: true,
  roundComplete: false,
  dayComplete: false,
  finalRound: false,
  focused: false,
  sessionState: 'idle',
});

const emit = defineEmits<{
  reveal: [];
  rate: [rating: AttemptRating];
  next: [];
  skip: [];
  pause: [];
  advance: [];
}>();

const ratingOptions: Array<{ value: AttemptRating; label: string; keyHint: string; className: string }> = [
  { value: 'forgotten', label: '忘了', keyHint: '1', className: 'border-err/30 bg-err/10 text-err hover:bg-err/15' },
  { value: 'fuzzy', label: '模糊', keyHint: '2', className: 'border-warn/30 bg-warn/10 text-warn hover:bg-warn/15' },
  { value: 'mastered', label: '掌握', keyHint: '3', className: 'border-ok/30 bg-ok/10 text-ok hover:bg-ok/15' },
];

const progress = computed(() => {
  if (!props.attempt || !props.attempt.total) return 0;
  const ratedCurrent = isAndroidApp && (props.attempt.completed || props.attempt.status === 'rated') ? 1 : 0;
  return Math.min(100, Math.max(0, ((props.attempt.position - 1 + ratedCurrent) / props.attempt.total) * 100));
});

const ratingLabel = (rating: AttemptRating): string => {
  const item = ratingOptions.find(option => option.value === rating);
  return item?.label ?? rating;
};

const handleKeyDown = (e: KeyboardEvent) => {
  if (props.busy) return;
  const target = e.target as HTMLElement | null;
  // Native buttons already activate on Enter/Space; do not also dispatch a study action.
  if (isAndroidApp && target?.closest('button, a, select, summary')) return;
  if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)) {
    return;
  }

  if (e.key === ' ' || e.code === 'Space' || e.key === 'Enter') {
    if (!props.attempt) {
      if (props.roundComplete && !props.dayComplete) {
        e.preventDefault();
        emit('advance');
      }
      return;
    }
    if (!props.attempt.revealed) {
      e.preventDefault();
      emit('reveal');
    } else if (props.attempt.completed || props.attempt.status === 'rated') {
      e.preventDefault();
      emit('next');
    }
  } else if (e.key === '1') {
    if (props.attempt?.revealed && !props.attempt.rating && !props.attempt.completed) {
      e.preventDefault();
      emit('rate', 'forgotten');
    }
  } else if (e.key === '2') {
    if (props.attempt?.revealed && !props.attempt.rating && !props.attempt.completed) {
      e.preventDefault();
      emit('rate', 'fuzzy');
    }
  } else if (e.key === '3') {
    if (props.attempt?.revealed && !props.attempt.rating && !props.attempt.completed) {
      e.preventDefault();
      emit('rate', 'mastered');
    }
  } else if (e.key === 'Escape') {
    if (props.attempt) {
      e.preventDefault();
      emit('pause');
    }
  }
};

onMounted(() => {
  window.addEventListener('keydown', handleKeyDown);
});

onUnmounted(() => {
  window.removeEventListener('keydown', handleKeyDown);
});
</script>

<style scoped>
.android-recall { min-width: 0; }
.android-recall :deep(.btn) { min-height: 48px; font-size: 14px; }
.android-recall .recall-question > p { font-size: 20px; line-height: 1.9; letter-spacing: .015em; }
.android-recall .recall-body { padding: 24px 20px; }
.android-recall .recall-answer > div > p { font-size: 16px; line-height: 1.9; }
.android-recall .recall-answer > div:last-child > div { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.android-recall .recall-answer button { min-height: 52px; font-size: 14px; border-radius: 12px; padding-inline: 8px; }
.android-recall > .surface:last-child button { min-height: 52px; }
.android-recall :deep(.surface) { overflow-wrap: anywhere; }
.android-recall.recall-focused .recall-body { min-height: 330px; }
@media (min-width: 840px) {
  .android-recall .recall-body { padding: 28px; }
  .android-recall.recall-focused .recall-body.has-answer { display: grid; grid-template-columns: minmax(0, .9fr) minmax(0, 1.1fr); gap: 24px; }
  .android-recall.recall-focused .recall-body.has-answer > .recall-answer { margin-top: 0; border-top: 0; border-left: 1px solid rgb(var(--c-line)); padding-top: 0; padding-left: 24px; }
  .android-recall .recall-question > p { font-size: 22px; }
  .android-recall.recall-focused .recall-body { min-height: 420px; }
}
</style>
