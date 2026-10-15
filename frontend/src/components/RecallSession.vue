<template>
  <section class="space-y-4" aria-labelledby="recall-session-title">
    <div class="surface p-4 sm:p-5 flex flex-wrap items-center justify-between gap-3">
      <div>
        <div class="flex items-center gap-2">
          <span class="inline-flex items-center rounded-full bg-accent/10 px-2 py-0.5 text-[11px] font-semibold text-accent">今日回忆</span>
          <span v-if="attempt && attempt.chapter_name" class="text-xs text-sub">{{ attempt.chapter_name }}</span>
        </div>
        <h2 id="recall-session-title" class="mt-1 text-lg font-bold tracking-tight text-ink">
          {{ attempt ? `第 ${attempt.position || 1} / ${attempt.total || 1} 张` : '准备开始' }}
        </h2>
      </div>
      <div class="flex items-center gap-2">
        <button v-if="attempt" type="button" class="btn btn-secondary text-xs" :disabled="busy" @click="emit('pause')">
          暂停 <kbd class="ml-1 opacity-60 font-mono text-[10px]">Esc</kbd>
        </button>
        <button v-if="attempt?.completed || attempt?.status === 'rated'" type="button" class="btn btn-secondary text-xs" :disabled="busy" @click="emit('skip')">跳过</button>
      </div>
    </div>

    <div v-if="!attempt" class="surface p-8 sm:p-12 text-center">
      <div class="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-ok/10 text-ok">
        <Check class="h-6 w-6" />
      </div>
      <h3 class="font-display text-lg font-semibold text-ink">{{ dayComplete ? '今日学习已完成' : roundComplete ? '本轮学习已完成' : '今日回忆已完成' }}</h3>
      <p class="mt-1 text-xs text-sub">{{ dayComplete ? '今天的任务已经完成，可以整理卡片或明天继续。' : roundComplete ? '可以进入下一轮，或返回卡片库整理内容。' : '可以返回卡片库整理内容，或稍后再来复习。' }}</p>
      <button v-if="roundComplete && !dayComplete" type="button" class="btn btn-primary mt-4" :disabled="busy" @click="emit('advance')">
        进入下一轮 <kbd class="ml-1.5 rounded bg-white/20 px-1.5 py-0.5 text-[10px] font-mono">Space</kbd>
      </button>
    </div>

    <template v-else>
      <div class="surface overflow-hidden border-t-4 border-t-accent">
        <div class="p-5 sm:p-8 space-y-5">
          <div class="flex items-start justify-between gap-3">
            <span class="text-xs font-semibold uppercase tracking-wider text-faint">问题</span>
            <span v-if="attempt.rating" class="rounded-full bg-ok/10 px-2 py-0.5 text-[11px] font-semibold text-ok">
              {{ ratingLabel(attempt.rating) }}
            </span>
          </div>
          <p class="whitespace-pre-wrap break-words font-display text-base sm:text-xl font-semibold leading-relaxed text-ink">{{ attempt.prompt }}</p>

          <div v-if="!attempt.revealed" class="rounded-xl border border-dashed border-accent/30 bg-accent/10 p-4 sm:p-5">
            <p class="text-sm leading-relaxed text-accent">先在心里或纸上作答，再揭示参考答案。主动回忆比重复阅读更有效。</p>
            <p v-if="showHints && attempt.hint" class="mt-2 text-xs text-accent"><span class="font-semibold">提示：</span>{{ attempt.hint }}</p>
            <button type="button" class="btn btn-primary mt-4 w-full sm:w-auto" :disabled="busy" @click="emit('reveal')">
              {{ busy ? '处理中…' : '揭示答案' }} <kbd v-if="!busy" class="ml-1.5 rounded bg-white/20 px-1.5 py-0.5 text-[10px] font-mono">Space</kbd>
            </button>
          </div>

          <div v-else class="space-y-4 border-t border-line pt-5">
            <div>
              <span class="text-xs font-semibold uppercase tracking-wider text-faint">参考答案</span>
              <p class="mt-2 whitespace-pre-wrap break-words text-sm sm:text-base leading-relaxed text-ink">{{ attempt.answer || '暂无参考答案' }}</p>
            </div>
            <div v-if="sourceExcerpt" class="rounded-xl border border-ok/30 bg-ok/10 p-3.5 text-xs text-ok">
              <span class="font-semibold">来源摘录：</span>
              <span class="whitespace-pre-wrap">{{ sourceExcerpt }}</span>
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
                  <span class="rounded bg-black/10 dark:bg-white/10 px-1.5 py-0.5 text-[10px] font-mono font-bold">{{ option.keyHint }}</span>
                  <span>{{ option.label }}</span>
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div class="surface p-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div class="text-xs text-sub">
          <span>完成进度</span>
          <div class="mt-1.5 h-2 w-full min-w-[180px] overflow-hidden rounded-full bg-raised sm:w-64">
            <div class="h-full rounded-full bg-brand-600 transition-all" :style="{ width: `${progress}%` }" />
          </div>
        </div>
        <button v-if="attempt.revealed && (attempt.completed || attempt.status === 'rated')" type="button" class="btn btn-primary" :disabled="busy" @click="emit('next')">
          下一张 <kbd class="ml-1.5 rounded bg-white/20 px-1.5 py-0.5 text-[10px] font-mono">Space</kbd>
        </button>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted } from 'vue';
import { Check } from 'lucide-vue-next';
import type { AttemptRating, TodayAttempt } from '../types/learning';

interface Props {
  attempt?: TodayAttempt | null;
  busy?: boolean;
  sourceExcerpt?: string | null;
  showHints?: boolean;
  roundComplete?: boolean;
  dayComplete?: boolean;
}

const props = withDefaults(defineProps<Props>(), {
  attempt: null,
  busy: false,
  sourceExcerpt: null,
  showHints: true,
  roundComplete: false,
  dayComplete: false,
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
  return Math.min(100, Math.max(0, ((props.attempt.position - 1) / props.attempt.total) * 100));
});

const ratingLabel = (rating: AttemptRating): string => {
  const item = ratingOptions.find(option => option.value === rating);
  return item?.label ?? rating;
};

const handleKeyDown = (e: KeyboardEvent) => {
  if (props.busy) return;
  const target = e.target as HTMLElement | null;
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
