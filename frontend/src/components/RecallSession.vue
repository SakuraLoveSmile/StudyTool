<template>
  <section class="space-y-4" aria-labelledby="recall-session-title">
    <div class="surface p-4 sm:p-5 flex flex-wrap items-center justify-between gap-3">
      <div>
        <div class="flex items-center gap-2">
          <span class="inline-flex items-center rounded-full bg-blue-50 px-2 py-0.5 text-[11px] font-semibold text-blue-700">今日回忆</span>
          <span v-if="attempt && attempt.chapter_name" class="text-xs text-slate-500">{{ attempt.chapter_name }}</span>
        </div>
        <h2 id="recall-session-title" class="mt-1 text-lg font-bold tracking-tight text-slate-900">
          {{ attempt ? `第 ${attempt.position || 1} / ${attempt.total || 1} 张` : '准备开始' }}
        </h2>
      </div>
      <div class="flex items-center gap-2">
        <button v-if="attempt" type="button" class="btn btn-secondary text-xs" :disabled="busy" @click="emit('pause')">暂停</button>
        <button v-if="attempt?.completed || attempt?.status === 'rated'" type="button" class="btn btn-secondary text-xs" :disabled="busy" @click="emit('skip')">跳过</button>
      </div>
    </div>

    <div v-if="!attempt" class="surface p-8 sm:p-12 text-center">
      <div class="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-slate-100 text-slate-500">✓</div>
      <h3 class="text-base font-semibold text-slate-800">今日回忆已完成</h3>
      <p class="mt-1 text-xs text-slate-500">可以返回卡片库整理内容，或稍后再来复习。</p>
    </div>

    <template v-else>
      <div class="surface overflow-hidden border-t-4 border-t-blue-600">
        <div class="p-5 sm:p-8 space-y-5">
          <div class="flex items-start justify-between gap-3">
            <span class="text-xs font-semibold uppercase tracking-wider text-slate-400">问题</span>
            <span v-if="attempt.rating" class="rounded-full bg-emerald-50 px-2 py-0.5 text-[11px] font-semibold text-emerald-700">
              {{ ratingLabel(attempt.rating) }}
            </span>
          </div>
          <p class="whitespace-pre-wrap break-words text-base sm:text-xl font-semibold leading-relaxed text-slate-900">{{ attempt.prompt }}</p>

          <div v-if="!attempt.revealed" class="rounded-xl border border-dashed border-blue-200 bg-blue-50/60 p-4 sm:p-5">
            <p class="text-sm leading-relaxed text-blue-900">先在心里或纸上作答，再揭示参考答案。主动回忆比重复阅读更有效。</p>
            <p v-if="attempt.hint" class="mt-2 text-xs text-blue-700"><span class="font-semibold">提示：</span>{{ attempt.hint }}</p>
            <button type="button" class="btn btn-primary mt-4 w-full sm:w-auto" :disabled="busy" @click="emit('reveal')">
              {{ busy ? '处理中…' : '揭示答案' }}
            </button>
          </div>

          <div v-else class="space-y-4 border-t border-slate-100 pt-5">
            <div>
              <span class="text-xs font-semibold uppercase tracking-wider text-slate-400">参考答案</span>
              <p class="mt-2 whitespace-pre-wrap break-words text-sm sm:text-base leading-relaxed text-slate-800">{{ attempt.answer || '暂无参考答案' }}</p>
            </div>
            <div v-if="sourceExcerpt" class="rounded-xl border border-emerald-100 bg-emerald-50/50 p-3.5 text-xs text-emerald-900">
              <span class="font-semibold">来源摘录：</span>
              <span class="whitespace-pre-wrap">{{ sourceExcerpt }}</span>
            </div>
            <div>
              <p class="mb-2 text-xs font-semibold text-slate-600">这次回忆的结果</p>
              <div class="grid grid-cols-1 gap-2 sm:grid-cols-3">
                <button
                  v-for="option in ratingOptions"
                  :key="option.value"
                  type="button"
                  class="min-h-[42px] rounded-lg border px-3 py-2 text-xs font-semibold transition-colors disabled:opacity-50"
                  :class="option.className"
                  :disabled="busy"
                  @click="emit('rate', option.value)"
                >
                  {{ option.label }}
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div class="surface p-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div class="text-xs text-slate-500">
          <span>完成进度</span>
          <div class="mt-1.5 h-2 w-full min-w-[180px] overflow-hidden rounded-full bg-slate-100 sm:w-64">
            <div class="h-full rounded-full bg-blue-600 transition-all" :style="{ width: `${progress}%` }" />
          </div>
        </div>
        <button v-if="attempt.revealed && (attempt.completed || attempt.status === 'rated')" type="button" class="btn btn-primary" :disabled="busy" @click="emit('next')">
          下一张
        </button>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import type { AttemptRating, TodayAttempt } from '../types/learning';

interface Props {
  attempt?: TodayAttempt | null;
  busy?: boolean;
  sourceExcerpt?: string | null;
}

const props = withDefaults(defineProps<Props>(), {
  attempt: null,
  busy: false,
  sourceExcerpt: null,
});

const emit = defineEmits<{
  reveal: [];
  rate: [rating: AttemptRating];
  next: [];
  skip: [];
  pause: [];
}>();

const ratingOptions: Array<{ value: AttemptRating; label: string; className: string }> = [
  { value: 'forgotten', label: '忘了', className: 'border-rose-200 bg-rose-50 text-rose-700 hover:bg-rose-100' },
  { value: 'fuzzy', label: '模糊', className: 'border-amber-200 bg-amber-50 text-amber-700 hover:bg-amber-100' },
  { value: 'mastered', label: '掌握', className: 'border-emerald-200 bg-emerald-50 text-emerald-700 hover:bg-emerald-100' },
];

const progress = computed(() => {
  if (!props.attempt || !props.attempt.total) return 0;
  return Math.min(100, Math.max(0, ((props.attempt.position - 1) / props.attempt.total) * 100));
});

const ratingLabel = (rating: AttemptRating): string => {
  const item = ratingOptions.find(option => option.value === rating);
  return item?.label ?? rating;
};
</script>
